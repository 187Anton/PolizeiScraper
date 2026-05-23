import argparse
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional
from urllib.parse import urldefrag, urljoin

import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


BASE_URL = "https://polizei.brandenburg.de"
OUTPUT_BASE_DIR = Path(os.getenv("SCRAPER_OUTPUT_DIR", "."))
SEARCH_URL_TEMPLATE = (
    "https://polizei.brandenburg.de/suche/typ/Meldungen/kategorie/null/1/{page}"
    "#pbb-search-result-pager"
)
SEARCH_URL_FALLBACK_TEMPLATE = (
    "https://polizei.brandenburg.de/suche/typ/Meldungen/kategorie/null/{page}/1"
    "#pbb-search-result-pager"
)
OUTPUT_CSV_PATH = OUTPUT_BASE_DIR / "data/raw/polizei_brandenburg_verkehr_raw.csv"
DAILY_OUTPUT = OUTPUT_BASE_DIR / "data/processed/polizei_verkehr_daily.csv"
CHECKPOINT_PATH = OUTPUT_BASE_DIR / "checkpoints/scraper_checkpoint.json"
LOG_PATH = OUTPUT_BASE_DIR / "logs/scraper.log"
RAW_OUTPUT = OUTPUT_CSV_PATH
YEARS_BACK = 3
REQUEST_DELAY_MIN = 0.8
REQUEST_DELAY_MAX = 2.0
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3
NO_NEW_URLS_THRESHOLD = 2
CAPTCHA_HINT = (
    "Falls ein Captcha erscheint, bitte im geöffneten Browser lösen und danach "
    "im Terminal Enter drücken."
)
TRAFFIC_CATEGORIES = {"verkehr", "verkehrsunfall"}
DIRECT_TRAFFIC_KEYWORDS = [
    "verkehr",
    "verkehrsunfall",
    "unfall",
    "fahrzeug",
    "pkw",
    "lkw",
    "auto",
    "motorrad",
    "fahrrad",
    "radfahrer",
    "e-scooter",
    "führerschein",
    "geschwindigkeit",
    "vorfahrt",
    "überhol",
    "bus",
]
STREET_CONTEXT_KEYWORD = "straße"
STREET_COMBO_KEYWORDS = {
    "verkehr",
    "verkehrsunfall",
    "unfall",
    "fahrzeug",
    "pkw",
    "lkw",
    "auto",
    "motorrad",
    "fahrrad",
    "radfahrer",
    "e-scooter",
    "führerschein",
    "geschwindigkeit",
    "vorfahrt",
    "überhol",
    "bus",
}
ALCOHOL_DRUG_KEYWORDS = {"alkohol", "drogen"}
ALCOHOL_DRUG_CONTEXT_KEYWORDS = {
    "fahrzeug",
    "pkw",
    "lkw",
    "auto",
    "motorrad",
    "fahrrad",
    "radfahrer",
    "e-scooter",
    "fahrer",
    "fahrerin",
    "straße",
    "verkehr",
    "verkehrsunfall",
    "unfall",
    "führerschein",
    "bus",
}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
GERMAN_WEEKDAYS = {
    "montag": 0,
    "dienstag": 1,
    "mittwoch": 2,
    "donnerstag": 3,
    "freitag": 4,
    "samstag": 5,
    "sonntag": 6,
}
WEEKDAY_PATTERN = "|".join(GERMAN_WEEKDAYS.keys())


@dataclass
class ArticleRecord:
    veroeffentlichungsdatum: date
    titel: str
    ort: str
    kategorie: str
    tags: str
    text: str
    url: str
    traffic_match_reason: str
    traffic_match_type: str
    ereignisdatum_start: Optional[date]
    ereignisdatum_ende: Optional[date]
    ereignisdatum_text: str
    ereignisdatum_confidence: str
    event_date_plausible: bool
    datum_fuer_modell: date
    datum_fuer_modell_quelle: str

    def to_row(self) -> dict[str, str]:
        return {
            "veroeffentlichungsdatum": self.veroeffentlichungsdatum.isoformat(),
            "titel": self.titel,
            "ort": self.ort,
            "kategorie": self.kategorie,
            "tags": self.tags,
            "text": self.text,
            "url": self.url,
            "traffic_match_reason": self.traffic_match_reason,
            "traffic_match_type": self.traffic_match_type,
            "ereignisdatum_start": (
                self.ereignisdatum_start.isoformat() if self.ereignisdatum_start else ""
            ),
            "ereignisdatum_ende": (
                self.ereignisdatum_ende.isoformat() if self.ereignisdatum_ende else ""
            ),
            "ereignisdatum_text": self.ereignisdatum_text,
            "ereignisdatum_confidence": self.ereignisdatum_confidence,
            "event_date_plausible": self.event_date_plausible,
            "datum_fuer_modell": self.datum_fuer_modell.isoformat(),
            "datum_fuer_modell_quelle": self.datum_fuer_modell_quelle,
        }


@dataclass
class ScrapeStats:
    visited_search_pages: int
    last_successful_page: int
    oldest_publication_date: Optional[date]
    newest_publication_date: Optional[date]
    oldest_model_date: Optional[date]
    newest_model_date: Optional[date]
    total_detail_urls_found: int
    stored_records_count: int
    duplicate_count: int
    stop_reason: str


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Scraped Polizeimeldungen der Polizei Brandenburg aus der Kategorie "
            "'Verkehr' und speichert Rohdaten sowie eine Tagesaggregation."
        )
    )
    parser.add_argument(
        "--start-date",
        help="Startdatum im Format YYYY-MM-DD. Standard: letzte 365 Tage.",
    )
    parser.add_argument(
        "--end-date",
        help="Enddatum im Format YYYY-MM-DD. Standard: heutiges Datum.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Browser unsichtbar starten. Nicht empfohlen, falls Captcha erscheint.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        help="Maximale Anzahl an Suchergebnisseiten, z. B. für Tests.",
    )
    parser.add_argument(
        "--allow-empty-overwrite",
        action="store_true",
        help="Erlaubt das Überschreiben bestehender CSV-Dateien auch bei 0 Ergebnissen.",
    )
    return parser


def parse_cli_date(value: Optional[str], option_name: str) -> Optional[date]:
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"Ungültiges Datum für {option_name}: {value}") from exc


def resolve_date_range(args: argparse.Namespace) -> tuple[date, date]:
    end_date = parse_cli_date(args.end_date, "--end-date") or date.today()
    start_date = parse_cli_date(args.start_date, "--start-date") or subtract_years(
        end_date, YEARS_BACK
    )
    if start_date > end_date:
        raise SystemExit("--start-date darf nicht nach --end-date liegen.")
    return start_date, end_date


def configure_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)


def ensure_output_directories() -> None:
    RAW_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    DAILY_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def random_wait(page) -> None:
    delay = random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)
    logging.debug("Warte %.2f Sekunden.", delay)
    page.wait_for_timeout(delay * 1000)


def normalize_whitespace(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def is_captcha_page(html: str) -> bool:
    lowered = html.lower()
    return "captcha" in lowered and "link11" in lowered


def prompt_for_captcha_resolution() -> None:
    try:
        input(f"{CAPTCHA_HINT}\n")
    except EOFError:
        logging.warning(
            "Kein interaktives Terminal verfügbar. Der Lauf kann erst nach "
            "manueller Captcha-Lösung fortgesetzt werden."
        )


def wait_for_page_ready(page, *, allow_manual_captcha: bool) -> str:
    for attempt in range(3):
        try:
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
            page.wait_for_timeout(1_500)
        except PlaywrightTimeoutError:
            logging.warning("Timeout beim Warten auf DOM-Inhalt, prüfe Seiteninhalt trotzdem.")

        html = page.content()
        if not is_captcha_page(html):
            return html

        if not allow_manual_captcha:
            raise RuntimeError(
                "Captcha erkannt, aber Browser läuft headless. Bitte ohne --headless starten."
            )

        logging.warning("Captcha erkannt. Warte auf manuelle Lösung im Browser.")
        prompt_for_captcha_resolution()
        page.wait_for_timeout(2_000)
        html = page.content()
        if not is_captcha_page(html):
            return html

        logging.warning("Captcha scheint noch aktiv zu sein. Neuer Prüfversuch (%s/3).", attempt + 1)

    raise RuntimeError("Captcha konnte nicht bestätigt werden.")


def goto_and_get_html(page, url: str, *, allow_manual_captcha: bool) -> str:
    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logging.info("Öffne %s", url)
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=REQUEST_TIMEOUT * 1000,
            )
            return wait_for_page_ready(page, allow_manual_captcha=allow_manual_captcha)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logging.warning(
                "Fehler beim Laden von %s (Versuch %s/%s): %s",
                url,
                attempt,
                MAX_RETRIES,
                exc,
            )
            if attempt < MAX_RETRIES:
                page.wait_for_timeout(1_500)
    raise RuntimeError(f"Seite konnte nicht geladen werden: {url}") from last_error


def extract_article_links(search_html: str) -> List[str]:
    soup = BeautifulSoup(search_html, "html.parser")
    seen: set[str] = set()
    links: List[str] = []

    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if "/pressemeldung/" not in href:
            continue
        absolute_url = urljoin(BASE_URL, href)
        if absolute_url in seen:
            continue
        seen.add(absolute_url)
        links.append(absolute_url)

    return links


def build_search_page_candidates(page_number: int) -> List[str]:
    candidates: List[str] = []
    seen_candidates: set[str] = set()
    for url in (
        SEARCH_URL_TEMPLATE.format(page=page_number),
        SEARCH_URL_FALLBACK_TEMPLATE.format(page=page_number),
    ):
        canonical_url = canonicalize_loop_url(url)
        if canonical_url in seen_candidates:
            continue
        seen_candidates.add(canonical_url)
        candidates.append(url)
    return candidates


def canonicalize_loop_url(url: str) -> str:
    return urldefrag(url)[0]


def extract_next_search_urls(search_html: str, current_page_number: int) -> List[str]:
    soup = BeautifulSoup(search_html, "html.parser")
    next_page_label = str(current_page_number + 1)
    candidate_urls: List[str] = []
    seen_candidates: set[str] = set()

    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if not href:
            continue

        absolute_url = urljoin(BASE_URL, href)
        anchor_text = normalize_whitespace(anchor.get_text(" ", strip=True))
        aria_label = normalize_whitespace(anchor.get("aria-label", ""))
        title = normalize_whitespace(anchor.get("title", ""))
        rel_values = " ".join(anchor.get("rel", []))

        if "/suche/" not in absolute_url:
            continue

        if anchor_text == next_page_label:
            canonical_url = canonicalize_loop_url(absolute_url)
            if canonical_url not in seen_candidates:
                seen_candidates.add(canonical_url)
                candidate_urls.append(absolute_url)
            continue

        if (
            "next" in rel_values.casefold()
            or "weiter" in aria_label.casefold()
            or "weiter" in title.casefold()
            or aria_label.casefold() == next_page_label.casefold()
        ):
            canonical_url = canonicalize_loop_url(absolute_url)
            if canonical_url not in seen_candidates:
                seen_candidates.add(canonical_url)
                candidate_urls.append(absolute_url)

    fallback_urls = [
        SEARCH_URL_TEMPLATE.format(page=current_page_number + 1),
        SEARCH_URL_FALLBACK_TEMPLATE.format(page=current_page_number + 1),
    ]
    for fallback_url in fallback_urls:
        canonical_url = canonicalize_loop_url(fallback_url)
        if canonical_url not in seen_candidates:
            seen_candidates.add(canonical_url)
            candidate_urls.append(fallback_url)

    return candidate_urls


def get_primary_content_root(soup: BeautifulSoup):
    h1 = soup.find("h1")
    if h1 is None:
        raise ValueError("Kein Artikeltitel gefunden.")

    for candidate in (h1.find_parent("main"), h1.find_parent("article"), h1.parent):
        if candidate is not None:
            return candidate
    return soup


def extract_lines_from_root(root) -> List[str]:
    return [
        normalize_whitespace(line)
        for line in root.get_text("\n", strip=True).splitlines()
        if normalize_whitespace(line)
    ]


def extract_value_after_label(lines: List[str], label: str) -> str:
    for index, line in enumerate(lines):
        if line == label and index + 1 < len(lines):
            return lines[index + 1]
    return ""


def extract_location(lines: List[str], title: str) -> str:
    metadata_labels = {"Kategorie", "Tags", "Datum", "Verantwortlich:"}
    try:
        title_index = lines.index(title)
    except ValueError:
        return ""

    for line in lines[title_index + 1 :]:
        normalized_line = normalize_whitespace(line)
        if not normalized_line:
            continue
        if normalized_line in metadata_labels:
            return ""
        return normalized_line

    return ""


def extract_tags(lines: List[str]) -> str:
    try:
        start_index = lines.index("Tags") + 1
    except ValueError:
        return ""

    collected: List[str] = []
    for line in lines[start_index:]:
        if line in {"Datum", "Kategorie", "Verantwortlich:", "Das könnte Sie auch interessieren"}:
            break
        collected.append(line)
    return " | ".join(collected)


def extract_body_text(lines: List[str]) -> str:
    try:
        start_index = lines.index("Datum") + 2
    except ValueError:
        raise ValueError("Datumsfeld nicht gefunden.")

    body_lines: List[str] = []
    stop_markers = {"Verantwortlich:", "Das könnte Sie auch interessieren"}
    for line in lines[start_index:]:
        if line in stop_markers:
            break
        body_lines.append(line)

    body_text = "\n".join(body_lines).strip()
    if not body_text:
        raise ValueError("Kein Meldungstext gefunden.")
    return body_text


def subtract_years(reference_date: date, years: int) -> date:
    try:
        return reference_date.replace(year=reference_date.year - years)
    except ValueError:
        return reference_date.replace(month=2, day=28, year=reference_date.year - years)


def parse_optional_date(value: object) -> Optional[date]:
    if value is None:
        return None
    text = normalize_whitespace(str(value))
    if not text or text.lower() == "nan":
        return None
    return datetime.strptime(text, "%Y-%m-%d").date()


def parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return normalize_whitespace(str(value)).lower() == "true"


def record_id_from_record(record: ArticleRecord) -> str:
    if record.url:
        return record.url
    return f"{record.titel}::{record.veroeffentlichungsdatum.isoformat()}"


def record_id_from_row(row: pd.Series) -> str:
    url = normalize_whitespace(str(row.get("url", "")))
    if url and url.lower() != "nan":
        return url
    title = normalize_whitespace(str(row.get("titel", "")))
    publication_date = normalize_whitespace(str(row.get("veroeffentlichungsdatum", "")))
    return f"{title}::{publication_date}"


def article_record_from_row(row: pd.Series) -> ArticleRecord:
    publication_date = parse_optional_date(row.get("veroeffentlichungsdatum"))
    model_date = parse_optional_date(row.get("datum_fuer_modell")) or publication_date
    if publication_date is None or model_date is None:
        raise ValueError("Bestehende Zeile enthält kein gültiges Veröffentlichungsdatum.")

    return ArticleRecord(
        veroeffentlichungsdatum=publication_date,
        titel=normalize_whitespace(str(row.get("titel", ""))),
        ort=normalize_whitespace(str(row.get("ort", "")))
        if str(row.get("ort", "")).lower() != "nan"
        else "",
        kategorie=normalize_whitespace(str(row.get("kategorie", ""))),
        tags=normalize_whitespace(str(row.get("tags", "")))
        if str(row.get("tags", "")).lower() != "nan"
        else "",
        text=str(row.get("text", "")) if str(row.get("text", "")).lower() != "nan" else "",
        url=normalize_whitespace(str(row.get("url", ""))),
        traffic_match_reason=str(row.get("traffic_match_reason", "")),
        traffic_match_type=str(row.get("traffic_match_type", "")),
        ereignisdatum_start=parse_optional_date(row.get("ereignisdatum_start")),
        ereignisdatum_ende=parse_optional_date(row.get("ereignisdatum_ende")),
        ereignisdatum_text=str(row.get("ereignisdatum_text", "")),
        ereignisdatum_confidence=str(row.get("ereignisdatum_confidence", "unknown")),
        event_date_plausible=parse_bool(row.get("event_date_plausible", False)),
        datum_fuer_modell=model_date,
        datum_fuer_modell_quelle=str(row.get("datum_fuer_modell_quelle", "publication_date")),
    )


def load_existing_records() -> dict[str, ArticleRecord]:
    if not RAW_OUTPUT.exists():
        return {}

    existing_df = pd.read_csv(RAW_OUTPUT)
    records_by_id: dict[str, ArticleRecord] = {}
    for _, row in existing_df.iterrows():
        try:
            record = article_record_from_row(row)
        except Exception as exc:  # noqa: BLE001
            logging.warning("Bestehende CSV-Zeile konnte nicht geladen werden: %s", exc)
            continue
        records_by_id[record_id_from_record(record)] = record

    logging.info("Bestehende Rohdaten geladen: %s Meldungen", len(records_by_id))
    return records_by_id


def load_checkpoint(start_date: date, end_date: date) -> dict:
    if not CHECKPOINT_PATH.exists():
        return {}

    try:
        checkpoint = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logging.warning("Checkpoint konnte nicht gelesen werden: %s", exc)
        return {}

    if (
        checkpoint.get("start_date") != start_date.isoformat()
        or checkpoint.get("end_date") != end_date.isoformat()
    ):
        logging.info("Checkpoint passt nicht zum aktuellen Zeitraum und wird ignoriert.")
        return {}

    return checkpoint


def atomic_write_dataframe(df: pd.DataFrame, path: Path) -> None:
    temp_path = path.with_name(f"{path.name}.tmp")
    df.to_csv(temp_path, index=False)
    temp_path.replace(path)


def atomic_write_json(data: dict, path: Path) -> None:
    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def parse_date_string(value: str) -> date:
    return datetime.strptime(value, "%d.%m.%Y").date()


def most_recent_weekday_on_or_before(reference_date: date, weekday: int) -> date:
    delta = (reference_date.weekday() - weekday) % 7
    return reference_date - timedelta(days=delta)


def resolve_weekday_range(
    publication_date: date, start_weekday: int, end_weekday: int
) -> tuple[date, date]:
    end_date = most_recent_weekday_on_or_before(publication_date, end_weekday)
    day_span = (end_weekday - start_weekday) % 7
    start_date = end_date - timedelta(days=day_span)
    return start_date, end_date


def previous_weekend(publication_date: date) -> tuple[date, date]:
    last_sunday = most_recent_weekday_on_or_before(publication_date - timedelta(days=1), 6)
    last_saturday = last_sunday - timedelta(days=1)
    return last_saturday, last_sunday


def is_event_date_plausible(event_date_info: dict, publication_date: date) -> bool:
    start_date = event_date_info["start_date"]
    if start_date is None:
        return False

    earliest_allowed_date = publication_date - timedelta(days=14)
    if start_date < earliest_allowed_date:
        return False
    if start_date > publication_date:
        return False
    return True


def build_model_date(
    event_date_info: dict, publication_date: date
) -> tuple[date, str, bool]:
    event_date_plausible = is_event_date_plausible(event_date_info, publication_date)
    if (
        event_date_info["confidence"] in {"high", "medium"}
        and event_date_info["start_date"] is not None
        and event_date_plausible
    ):
        return event_date_info["start_date"], "event_date", True

    if event_date_info["start_date"] is not None and not event_date_plausible:
        return (
            publication_date,
            "publication_date_fallback_unplausible_event_date",
            False,
        )

    return publication_date, "publication_date", False


def extract_event_date(text: str, publication_date: date) -> dict:
    normalized_text = normalize_whitespace(text)

    explicit_weekday_date_patterns = [
        rf"((?:am\s+)?({WEEKDAY_PATTERN})(?:morgen|abend|nacht|mittag|nachmittag)?\s*,\s*\d{{2}}\.\d{{2}}\.\d{{4}}(?:,\s*\d{{1,2}}:\d{{2}}\s*uhr)?)",
        rf"((?:am\s+)?({WEEKDAY_PATTERN})(?:morgen|abend|nacht|mittag|nachmittag)?\s*\(\s*\d{{2}}\.\d{{2}}\.\d{{4}}\s*\))",
    ]
    for pattern in explicit_weekday_date_patterns:
        match = re.search(pattern, normalized_text, flags=re.IGNORECASE)
        if match:
            matched_text = normalized_text[match.start() : match.end()]
            date_match = re.search(r"\d{2}\.\d{2}\.\d{4}", matched_text)
            if date_match:
                parsed_date = parse_date_string(date_match.group(0))
                return {
                    "start_date": parsed_date,
                    "end_date": parsed_date,
                    "matched_text": matched_text,
                    "confidence": "high",
                }

    parenthesized_date_match = re.search(
        r"(\(\s*\d{2}\.\d{2}\.\d{4}\s*\))", normalized_text, flags=re.IGNORECASE
    )
    if parenthesized_date_match:
        matched_text = normalized_text[
            parenthesized_date_match.start() : parenthesized_date_match.end()
        ]
        date_match = re.search(r"\d{2}\.\d{2}\.\d{4}", matched_text)
        if date_match:
            parsed_date = parse_date_string(date_match.group(0))
            return {
                "start_date": parsed_date,
                "end_date": parsed_date,
                "matched_text": matched_text,
                "confidence": "high",
            }

    explicit_range_patterns = [
        r"(zwischen dem\s+\d{2}\.\d{2}\.\d{4}\s+und\s+\d{2}\.\d{2}\.\d{4})",
        r"(in der nacht vom\s+\d{2}\.\d{2}\.\d{4}\s+zum\s+\d{2}\.\d{2}\.\d{4})",
    ]
    for pattern in explicit_range_patterns:
        match = re.search(pattern, normalized_text, flags=re.IGNORECASE)
        if match:
            matched_text = normalized_text[match.start() : match.end()]
            date_matches = re.findall(r"\d{2}\.\d{2}\.\d{4}", matched_text)
            if len(date_matches) >= 2:
                return {
                    "start_date": parse_date_string(date_matches[0]),
                    "end_date": parse_date_string(date_matches[1]),
                    "matched_text": matched_text,
                    "confidence": "high",
                }

    explicit_single_patterns = [
        r"(am\s+\d{2}\.\d{2}\.\d{4})",
        r"(vom\s+\d{2}\.\d{2}\.\d{4})",
        r"(\d{2}\.\d{2}\.\d{4}(?:,\s*\d{1,2}:\d{2}\s*uhr)?)",
    ]
    for pattern in explicit_single_patterns:
        match = re.search(pattern, normalized_text, flags=re.IGNORECASE)
        if match:
            matched_text = normalized_text[match.start() : match.end()]
            date_match = re.search(r"\d{2}\.\d{2}\.\d{4}", matched_text)
            if date_match:
                parsed_date = parse_date_string(date_match.group(0))
                return {
                    "start_date": parsed_date,
                    "end_date": parsed_date,
                    "matched_text": matched_text,
                    "confidence": "high",
                }

    relative_patterns = {
        "vorgestern": publication_date - timedelta(days=2),
        "gestern": publication_date - timedelta(days=1),
        "heute": publication_date,
    }
    for phrase, resolved_date in relative_patterns.items():
        match = re.search(rf"\b{phrase}\b", normalized_text, flags=re.IGNORECASE)
        if match:
            return {
                "start_date": resolved_date,
                "end_date": resolved_date,
                "matched_text": normalized_text[match.start() : match.end()],
                "confidence": "medium",
            }

    weekend_match = re.search(
        r"(am vergangenen wochenende)", normalized_text, flags=re.IGNORECASE
    )
    if weekend_match:
        start_date, end_date = previous_weekend(publication_date)
        return {
            "start_date": start_date,
            "end_date": end_date,
            "matched_text": normalized_text[weekend_match.start() : weekend_match.end()],
            "confidence": "low",
        }

    weekday_range_patterns = [
        ("range", rf"(zwischen\s+({WEEKDAY_PATTERN})\s+und\s+({WEEKDAY_PATTERN}))"),
        (
            "range",
            rf"(in der nacht von\s+({WEEKDAY_PATTERN})\s+zu\s+({WEEKDAY_PATTERN}))",
        ),
        ("single", rf"(in der nacht zu\s+({WEEKDAY_PATTERN}))"),
    ]
    for pattern_type, pattern in weekday_range_patterns:
        match = re.search(pattern, normalized_text, flags=re.IGNORECASE)
        if match:
            if pattern_type == "single":
                weekday_name = match.group(2).casefold()
                resolved_date = most_recent_weekday_on_or_before(
                    publication_date, GERMAN_WEEKDAYS[weekday_name]
                )
                return {
                    "start_date": resolved_date,
                    "end_date": resolved_date,
                    "matched_text": normalized_text[match.start() : match.end()],
                    "confidence": "medium",
                }

            start_name = match.group(2).casefold()
            end_name = match.group(3).casefold()
            start_date, end_date = resolve_weekday_range(
                publication_date,
                GERMAN_WEEKDAYS[start_name],
                GERMAN_WEEKDAYS[end_name],
            )
            return {
                "start_date": start_date,
                "end_date": end_date,
                "matched_text": normalized_text[match.start() : match.end()],
                "confidence": "medium",
            }

    weekday_match = re.search(
        rf"((?:am\s+)?({WEEKDAY_PATTERN})(?:morgen|abend|nacht|mittag|nachmittag)?(?:\s+gegen\s+\d{{1,2}}:\d{{2}}\s*uhr)?)",
        normalized_text,
        flags=re.IGNORECASE,
    )
    if weekday_match:
        weekday_name = weekday_match.group(2).casefold()
        resolved_date = most_recent_weekday_on_or_before(
            publication_date, GERMAN_WEEKDAYS[weekday_name]
        )
        return {
            "start_date": resolved_date,
            "end_date": resolved_date,
            "matched_text": normalized_text[weekday_match.start() : weekday_match.end()],
            "confidence": "medium",
        }

    return {
        "start_date": None,
        "end_date": None,
        "matched_text": "",
        "confidence": "unknown",
    }


def parse_article(article_html: str, url: str) -> ArticleRecord:
    soup = BeautifulSoup(article_html, "html.parser")
    root = get_primary_content_root(soup)

    title_element = root.find("h1")
    if title_element is None:
        raise ValueError("Titel konnte nicht extrahiert werden.")

    title = normalize_whitespace(title_element.get_text(" ", strip=True))
    lines = extract_lines_from_root(root)
    location = extract_location(lines, title)

    category = normalize_whitespace(extract_value_after_label(lines, "Kategorie"))
    date_text = normalize_whitespace(extract_value_after_label(lines, "Datum"))
    tags = extract_tags(lines)
    body_text = extract_body_text(lines)

    if not category:
        raise ValueError("Kategorie konnte nicht extrahiert werden.")
    if not date_text:
        raise ValueError("Datum konnte nicht extrahiert werden.")

    publication_date = datetime.strptime(date_text, "%d.%m.%Y").date()
    event_date_info = extract_event_date(body_text, publication_date)
    model_date, model_date_source, event_date_plausible = build_model_date(
        event_date_info, publication_date
    )

    return ArticleRecord(
        veroeffentlichungsdatum=publication_date,
        titel=title,
        ort=location,
        kategorie=category,
        tags=tags,
        text=body_text,
        url=url,
        traffic_match_reason="",
        traffic_match_type="",
        ereignisdatum_start=event_date_info["start_date"],
        ereignisdatum_ende=event_date_info["end_date"],
        ereignisdatum_text=event_date_info["matched_text"],
        ereignisdatum_confidence=event_date_info["confidence"],
        event_date_plausible=event_date_plausible,
        datum_fuer_modell=model_date,
        datum_fuer_modell_quelle=model_date_source,
    )


def get_traffic_match_reason(article: ArticleRecord) -> str:
    normalized_category = normalize_whitespace(article.kategorie).casefold()
    if normalized_category in TRAFFIC_CATEGORIES:
        return f"category: {article.kategorie}"

    searchable_text = " ".join([article.titel, article.tags, article.text]).casefold()
    for keyword in DIRECT_TRAFFIC_KEYWORDS:
        if keyword.casefold() in searchable_text:
            return f"keyword: {keyword}"

    if STREET_CONTEXT_KEYWORD in searchable_text:
        for context_keyword in STREET_COMBO_KEYWORDS:
            if context_keyword in searchable_text:
                return f"keyword_combo: {STREET_CONTEXT_KEYWORD} + {context_keyword}"

    found_substance_keywords = [
        keyword for keyword in ALCOHOL_DRUG_KEYWORDS if keyword in searchable_text
    ]
    if found_substance_keywords:
        for context_keyword in ALCOHOL_DRUG_CONTEXT_KEYWORDS:
            if context_keyword in searchable_text:
                return f"keyword_combo: {found_substance_keywords[0]} + {context_keyword}"

    return ""


def get_traffic_match_type(article: ArticleRecord) -> str:
    normalized_category = normalize_whitespace(article.kategorie).casefold()
    if normalized_category in TRAFFIC_CATEGORIES:
        return "category"

    if get_traffic_match_reason(article):
        return "keyword"

    return ""


def is_traffic_related(article: ArticleRecord) -> bool:
    return bool(get_traffic_match_reason(article))


def save_raw_data(records: Iterable[ArticleRecord]) -> pd.DataFrame:
    df = pd.DataFrame([record.to_row() for record in records])
    if df.empty:
        df = pd.DataFrame(
            columns=[
                "veroeffentlichungsdatum",
                "titel",
                "ort",
                "kategorie",
                "tags",
                "text",
                "url",
                "traffic_match_reason",
                "traffic_match_type",
                "ereignisdatum_start",
                "ereignisdatum_ende",
                "ereignisdatum_text",
                "ereignisdatum_confidence",
                "event_date_plausible",
                "datum_fuer_modell",
                "datum_fuer_modell_quelle",
            ]
        )
    else:
        df = df.sort_values(["datum_fuer_modell", "url"]).drop_duplicates(
            subset=["url"]
        ).reset_index(drop=True)

    atomic_write_dataframe(df, RAW_OUTPUT)
    logging.info("Rohdaten gespeichert: %s", RAW_OUTPUT)
    return df


def save_daily_aggregation(raw_df: pd.DataFrame, start_date: date, end_date: date) -> None:
    date_index = pd.date_range(start=start_date, end=end_date, freq="D")

    if raw_df.empty:
        daily_df = pd.DataFrame({"datum": date_index})
        daily_df["anzahl_verkehrsmeldungen"] = 0
        daily_df["anzahl_category_matches"] = 0
        daily_df["anzahl_keyword_matches"] = 0
    else:
        temp_df = raw_df.copy()
        temp_df["datum_fuer_modell"] = pd.to_datetime(temp_df["datum_fuer_modell"])
        temp_df["is_category_match"] = (temp_df["traffic_match_type"] == "category").astype(int)
        temp_df["is_keyword_match"] = (temp_df["traffic_match_type"] == "keyword").astype(int)
        daily_df = temp_df.groupby("datum_fuer_modell").agg(
            anzahl_verkehrsmeldungen=("url", "size"),
            anzahl_category_matches=("is_category_match", "sum"),
            anzahl_keyword_matches=("is_keyword_match", "sum"),
        )
        daily_df = (
            daily_df.reindex(date_index, fill_value=0)
            .rename_axis("datum")
            .reset_index()
        )

    daily_df["verkehrsmeldung_vorhanden"] = (
        daily_df["anzahl_verkehrsmeldungen"] > 0
    ).astype(int)
    daily_df["datum"] = daily_df["datum"].dt.strftime("%Y-%m-%d")
    atomic_write_dataframe(daily_df, DAILY_OUTPUT)
    logging.info("Tagesaggregation gespeichert: %s", DAILY_OUTPUT)


def save_checkpoint(
    *,
    last_successful_page: int,
    start_date: date,
    end_date: date,
    stored_records_count: int,
) -> None:
    checkpoint_data = {
        "last_successful_page": last_successful_page,
        "last_run_at": datetime.now().isoformat(timespec="seconds"),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "stored_records_count": stored_records_count,
    }
    atomic_write_json(checkpoint_data, CHECKPOINT_PATH)
    logging.info("Checkpoint gespeichert: %s", CHECKPOINT_PATH)


def persist_progress(
    records: Iterable[ArticleRecord],
    *,
    start_date: date,
    end_date: date,
    last_successful_page: int,
) -> pd.DataFrame:
    raw_df = save_raw_data(records)
    save_daily_aggregation(raw_df, start_date, end_date)
    save_checkpoint(
        last_successful_page=last_successful_page,
        start_date=start_date,
        end_date=end_date,
        stored_records_count=len(raw_df),
    )
    return raw_df


def should_skip_empty_overwrite(
    records: Iterable[ArticleRecord], allow_empty_overwrite: bool
) -> bool:
    record_count = len(list(records)) if not isinstance(records, list) else len(records)
    if record_count > 0:
        return False

    logging.warning("Keine Meldungen gefunden.")
    if allow_empty_overwrite:
        logging.warning(
            "Leere Ergebnisdateien werden geschrieben, weil --allow-empty-overwrite gesetzt ist."
        )
        return False

    existing_outputs = [path for path in (RAW_OUTPUT, DAILY_OUTPUT) if path.exists()]
    if existing_outputs:
        logging.warning(
            "Keine Meldungen gefunden. Bestehende Ausgabedateien wurden nicht überschrieben."
        )
        return True

    logging.warning(
        "Keine Meldungen gefunden. Es existieren keine bisherigen Ausgabedateien zum Schützen."
    )
    return False


def scrape_articles(
    start_date: date,
    end_date: date,
    *,
    headless: bool,
    max_pages: Optional[int],
    existing_records_by_id: Optional[dict[str, ArticleRecord]] = None,
    start_page: int = 1,
) -> tuple[List[ArticleRecord], ScrapeStats]:
    records_by_id = dict(existing_records_by_id or {})
    records: List[ArticleRecord] = list(records_by_id.values())
    seen_urls: set[str] = {record.url for record in records if record.url}
    stop_reason = "unbekannt"
    visited_search_urls: set[str] = set()
    visited_search_pages = 0
    all_detail_urls_found: set[str] = set()
    last_successful_page = max(start_page - 1, 0)
    oldest_publication_date: Optional[date] = min(
        (record.veroeffentlichungsdatum for record in records), default=None
    )
    newest_publication_date: Optional[date] = max(
        (record.veroeffentlichungsdatum for record in records), default=None
    )
    oldest_model_date: Optional[date] = min(
        (record.datum_fuer_modell for record in records), default=None
    )
    newest_model_date: Optional[date] = max(
        (record.datum_fuer_modell for record in records), default=None
    )
    cached_article_records: dict[str, ArticleRecord] = {}
    consecutive_pages_without_new_urls = 0
    duplicate_count = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=USER_AGENT, locale="de-DE")
        search_page = context.new_page()
        detail_page = context.new_page()

        print(CAPTCHA_HINT)
        sys.stdout.flush()

        page_number = start_page
        while True:
            if max_pages is not None and page_number > max_pages:
                logging.info("Maximale Seitenzahl erreicht: %s", max_pages)
                stop_reason = "max_pages erreicht"
                break

            candidate_results = []
            for candidate_url in build_search_page_candidates(page_number):
                canonical_candidate_url = canonicalize_loop_url(candidate_url)
                if canonical_candidate_url in visited_search_urls:
                    continue

                visited_search_urls.add(canonical_candidate_url)
                search_html = goto_and_get_html(
                    search_page,
                    candidate_url,
                    allow_manual_captcha=not headless,
                )
                visited_search_pages += 1
                article_urls = extract_article_links(search_html)
                new_urls_on_page = sum(1 for url in article_urls if url not in seen_urls)
                duplicate_urls_on_page = len(article_urls) - new_urls_on_page
                candidate_results.append(
                    {
                        "requested_url": candidate_url,
                        "actual_url": search_page.url,
                        "article_urls": article_urls,
                        "article_url_set": set(article_urls),
                        "new_urls_on_page": new_urls_on_page,
                        "duplicate_urls_on_page": duplicate_urls_on_page,
                    }
                )

            if not candidate_results:
                logging.info("Keine nächste unbesuchte Suchseiten-URL mehr gefunden.")
                stop_reason = "keine nächste Seite"
                break

            if len(candidate_results) > 1 and (
                candidate_results[0]["article_url_set"] == candidate_results[1]["article_url_set"]
            ):
                logging.warning("Pagination scheint auf derselben Ergebnisseite zu bleiben.")

            selected_candidate = max(
                candidate_results,
                key=lambda item: (item["new_urls_on_page"], len(item["article_urls"])),
            )
            article_urls = selected_candidate["article_urls"]
            all_detail_urls_found.update(article_urls)

            if not article_urls:
                logging.info("Keine Artikel-Links mehr gefunden. Scraping wird beendet.")
                stop_reason = "keine Artikel-Links"
                break

            page_oldest_publication_date: Optional[date] = None
            page_newest_publication_date: Optional[date] = None
            page_new_urls_processed = 0
            page_reached_before_start_date = False
            page_duplicate_count = 0
            for article_url in article_urls:
                cached_record = cached_article_records.get(article_url)
                if cached_record is not None:
                    record = cached_record
                else:
                    random_wait(detail_page)
                    try:
                        article_html = goto_and_get_html(
                            detail_page,
                            article_url,
                            allow_manual_captcha=not headless,
                        )
                        record = parse_article(article_html, article_url)
                        cached_article_records[article_url] = record
                    except Exception as exc:  # noqa: BLE001
                        logging.exception("Fehler bei %s: %s", article_url, exc)
                        continue

                if (
                    page_oldest_publication_date is None
                    or record.veroeffentlichungsdatum < page_oldest_publication_date
                ):
                    page_oldest_publication_date = record.veroeffentlichungsdatum
                if (
                    page_newest_publication_date is None
                    or record.veroeffentlichungsdatum > page_newest_publication_date
                ):
                    page_newest_publication_date = record.veroeffentlichungsdatum

                if (
                    oldest_publication_date is None
                    or record.veroeffentlichungsdatum < oldest_publication_date
                ):
                    oldest_publication_date = record.veroeffentlichungsdatum
                if (
                    newest_publication_date is None
                    or record.veroeffentlichungsdatum > newest_publication_date
                ):
                    newest_publication_date = record.veroeffentlichungsdatum
                if oldest_model_date is None or record.datum_fuer_modell < oldest_model_date:
                    oldest_model_date = record.datum_fuer_modell
                if newest_model_date is None or record.datum_fuer_modell > newest_model_date:
                    newest_model_date = record.datum_fuer_modell

                if record.veroeffentlichungsdatum < start_date:
                    page_reached_before_start_date = True

                record_id = record_id_from_record(record)
                if record_id in records_by_id:
                    duplicate_count += 1
                    page_duplicate_count += 1
                    continue

                seen_urls.add(article_url)
                page_new_urls_processed += 1

                if record.veroeffentlichungsdatum < start_date:
                    logging.info(
                        "Meldung %s liegt vor dem Startdatum (%s < %s). "
                        "Wird nicht gespeichert.",
                        record.url,
                        record.veroeffentlichungsdatum,
                        start_date,
                    )
                    continue

                if record.veroeffentlichungsdatum > end_date:
                    logging.info(
                        "Meldung %s liegt nach dem Enddatum (%s > %s) und wird übersprungen.",
                        record.url,
                        record.veroeffentlichungsdatum,
                        end_date,
                    )
                    continue

                traffic_match_reason = get_traffic_match_reason(record)
                if not is_traffic_related(record):
                    logging.info(
                        "Meldung übersprungen: %s | Kategorie=%s | kein Verkehrs-Match in "
                        "Kategorie, Titel, Tags oder Text gefunden.",
                        record.url,
                        record.kategorie,
                    )
                    continue

                record.traffic_match_reason = traffic_match_reason
                record.traffic_match_type = get_traffic_match_type(record)
                records_by_id[record_id] = record
                records.append(record)
                logging.info(
                    "Meldung übernommen: veröff=%s | modell=%s (%s) | Ort=%s | %s | %s | %s | %s",
                    record.veroeffentlichungsdatum.isoformat(),
                    record.datum_fuer_modell.isoformat(),
                    record.datum_fuer_modell_quelle,
                    record.ort,
                    record.kategorie,
                    record.titel,
                    record.traffic_match_type,
                    record.traffic_match_reason,
                )

            logging.info(
                "Suchseite %s ausgewählt: requested=%s | actual=%s | seitennummer=%s | "
                "artikel_links=%s | neue_artikel_links=%s | duplikate=%s | "
                "ältestes_veröffentlichungsdatum=%s | "
                "neuestes_veröffentlichungsdatum=%s",
                page_number,
                selected_candidate["requested_url"],
                selected_candidate["actual_url"],
                page_number,
                len(article_urls),
                page_new_urls_processed,
                page_duplicate_count,
                page_oldest_publication_date.isoformat()
                if page_oldest_publication_date
                else "n/a",
                page_newest_publication_date.isoformat()
                if page_newest_publication_date
                else "n/a",
            )

            if page_new_urls_processed == 0:
                consecutive_pages_without_new_urls += 1
                logging.info(
                    "Keine neuen URLs auf Seite %s. Zähler=%s",
                    page_number,
                    consecutive_pages_without_new_urls,
                )
            else:
                consecutive_pages_without_new_urls = 0

            persist_progress(
                records_by_id.values(),
                start_date=start_date,
                end_date=end_date,
                last_successful_page=page_number,
            )
            last_successful_page = page_number
            logging.info(
                "Gesamtanzahl gespeicherter Meldungen nach Seite %s: %s",
                page_number,
                len(records_by_id),
            )
            logging.info("Übersprungene Duplikate gesamt: %s", duplicate_count)

            if page_reached_before_start_date:
                stop_reason = "Startdatum unterschritten"
                logging.info(
                    "Cutoff-Date erreicht. Älteste Meldungen auf Seite %s liegen vor %s.",
                    page_number,
                    start_date,
                )
                break

            if consecutive_pages_without_new_urls >= NO_NEW_URLS_THRESHOLD:
                stop_reason = "mehrere Seiten hintereinander keine neuen URLs"
                break

            page_number += 1

        context.close()
        browser.close()

    stats = ScrapeStats(
        visited_search_pages=visited_search_pages,
        last_successful_page=last_successful_page,
        oldest_publication_date=oldest_publication_date,
        newest_publication_date=newest_publication_date,
        oldest_model_date=oldest_model_date,
        newest_model_date=newest_model_date,
        total_detail_urls_found=len(all_detail_urls_found),
        stored_records_count=len(records_by_id),
        duplicate_count=duplicate_count,
        stop_reason=stop_reason,
    )
    return list(records_by_id.values()), stats


def main() -> None:
    configure_logging()
    parser = build_argument_parser()
    args = parser.parse_args()
    start_date, end_date = resolve_date_range(args)
    ensure_output_directories()
    existing_records_by_id = load_existing_records()
    checkpoint = load_checkpoint(start_date, end_date)
    start_page = int(checkpoint.get("last_successful_page", 0)) + 1 if checkpoint else 1

    logging.info("Startzeit: %s", datetime.now().isoformat(timespec="seconds"))
    logging.info("Untersuchungszeitraum: %s bis %s", start_date, end_date)
    logging.info("cutoff_date: %s", start_date)
    logging.info("Browsermodus: %s", "headless" if args.headless else "sichtbar")
    logging.info("Startseite für diesen Lauf: %s", start_page)
    logging.info("Vorhandene Meldungen vor Resume: %s", len(existing_records_by_id))
    if checkpoint:
        logging.info("Resume mit Checkpoint: %s", checkpoint)

    started_at = time.perf_counter()
    records, stats = scrape_articles(
        start_date=start_date,
        end_date=end_date,
        headless=args.headless,
        max_pages=args.max_pages,
        existing_records_by_id=existing_records_by_id,
        start_page=start_page,
    )
    logging.info("Anzahl besuchter Suchseiten: %s", stats.visited_search_pages)
    logging.info(
        "Ältestes gefundenes Veröffentlichungsdatum: %s",
        stats.oldest_publication_date.isoformat() if stats.oldest_publication_date else "n/a",
    )
    logging.info(
        "Neuestes gefundenes Veröffentlichungsdatum: %s",
        stats.newest_publication_date.isoformat() if stats.newest_publication_date else "n/a",
    )
    logging.info(
        "Ältestes Modelldatum: %s",
        stats.oldest_model_date.isoformat() if stats.oldest_model_date else "n/a",
    )
    logging.info(
        "Neuestes Modelldatum: %s",
        stats.newest_model_date.isoformat() if stats.newest_model_date else "n/a",
    )
    logging.info("Anzahl gefundener Detail-URLs insgesamt: %s", stats.total_detail_urls_found)
    logging.info("Anzahl gespeicherter Meldungen: %s", stats.stored_records_count)
    logging.info("Übersprungene Duplikate gesamt: %s", stats.duplicate_count)
    logging.info("Abbruchgrund: %s", stats.stop_reason)

    if should_skip_empty_overwrite(records, args.allow_empty_overwrite):
        elapsed = time.perf_counter() - started_at
        logging.info(
            "Scraping abgeschlossen: 0 Meldungen gespeichert in %.1f Sekunden.",
            elapsed,
        )
        return

    raw_df = persist_progress(
        records,
        start_date=start_date,
        end_date=end_date,
        last_successful_page=stats.last_successful_page,
    )

    elapsed = time.perf_counter() - started_at
    logging.info(
        "Scraping abgeschlossen: %s Meldungen gespeichert in %.1f Sekunden.",
        len(raw_df),
        elapsed,
    )


if __name__ == "__main__":
    main()
