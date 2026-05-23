# Scraper Polizei Brandenburg

Dieses Projekt erhebt Polizeimeldungen der Polizei Brandenburg direkt über die Such- und Detailseiten. Der RSS-Feed wird bewusst nicht verwendet, weil dort laut Aufgabenstellung nicht alle relevanten Meldungen auftauchen.

## Projektstruktur

```text
data/
  raw/
  processed/
logs/
src/
  data_collection/
    scrape_polizei_brandenburg.py
requirements.txt
README.md
```

## Installation

1. Optional eine virtuelle Umgebung anlegen und aktivieren.
2. Abhängigkeiten installieren:

```bash
python -m pip install -r requirements.txt
```

## Playwright-Setup

Für Playwright muss zusätzlich ein Browser installiert werden:

```bash
python -m playwright install chromium
```

Unter macOS kann Playwright beim ersten Setup weitere Systemhinweise anzeigen. In diesem Fall die Hinweise im Terminal befolgen.

## Was der Scraper macht

- Öffnet die Such-/Ergebnisseiten der Polizei Brandenburg mit Playwright.
- Sammelt Links zu Pressemeldungs-Detailseiten.
- Lädt die Detailseiten und parst sie mit BeautifulSoup.
- Berücksichtigt verkehrsbezogene Meldungen auf Basis von Kategorien und Textmerkmalen.
- Filtert standardmäßig auf die letzten 3 Jahre.
- Dedupliziert Meldungen anhand der URL.
- Stoppt die Pagination, sobald ältere Meldungen als das Cutoff-Datum erreicht werden.
- Speichert Rohdaten und eine Tagesaggregation als CSV.
- Schreibt nach jeder erfolgreich verarbeiteten Suchseite einen Zwischenstand.
- Legt einen Checkpoint an, sodass ein abgebrochener Lauf fortgesetzt werden kann.

Zusätzlich unterscheidet der Scraper zwischen:

- `veroeffentlichungsdatum`: das Datum, das direkt auf der Polizeimeldungsseite steht
- `ereignisdatum_*`: ein heuristisch aus dem Meldungstext abgeleitetes Ereignisdatum
- `datum_fuer_modell`: das Datum, das für spätere Modellierung und die Tagesaggregation verwendet wird

Das Ereignisdatum wird nur heuristisch aus typischen deutschen Formulierungen im Meldungstext extrahiert, zum Beispiel aus expliziten Datumsangaben, relativen Angaben wie `gestern` oder Wochentagen wie `am Dienstag`. Wenn kein ausreichend belastbares Ereignisdatum erkannt wird, fällt der Scraper für `datum_fuer_modell` auf das Veröffentlichungsdatum zurück.

Beim Start wird diese Hinweiszeile ausgegeben:

> Falls ein Captcha erscheint, bitte im geöffneten Browser lösen und danach im Terminal Enter drücken.

Standardmäßig läuft der Browser sichtbar, damit ein mögliches Captcha manuell gelöst werden kann. Mit `--headless` kann der Browser unsichtbar gestartet werden. Wenn dann ein Captcha erscheint, muss der Lauf ohne `--headless` wiederholt werden.

## Wiederaufnahme und Stabilität

- Der Standardscrape läuft über die letzten 3 Jahre.
- Nach jeder erfolgreich verarbeiteten Suchseite werden Rohdaten, Tagesaggregation und ein Checkpoint gespeichert.
- Beim Neustart lädt der Scraper vorhandene Rohdaten und setzt den Lauf anhand des Checkpoints fort.
- Dedupliziert wird bevorzugt über die Meldungs-URL, sonst über `Titel + Veröffentlichungsdatum`.
- Log-Ausgaben landen sowohl in der Konsole als auch in `logs/scraper.log`.

Standardpfade:

- Rohdaten: `data/raw/polizei_brandenburg_verkehr_raw.csv`
- Checkpoint: `checkpoints/scraper_checkpoint.json`
- Tagesaggregation: `data/processed/polizei_verkehr_daily.csv`
- Log-Datei: `logs/scraper.log`

Optional kann das Basisverzeichnis über `SCRAPER_OUTPUT_DIR` umgelegt werden. Dann werden die Dateien relativ zu diesem Verzeichnis erzeugt, zum Beispiel:

- `data/raw/polizei_brandenburg_verkehr_raw.csv`
- `data/processed/polizei_verkehr_daily.csv`
- `checkpoints/scraper_checkpoint.json`
- `logs/scraper.log`

Beispiel für iCloud/OneDrive/Dropbox:

```bash
SCRAPER_OUTPUT_DIR="/Users/anton/Library/Mobile Documents/com~apple~CloudDocs/PolizeiScraper" \
python src/data_collection/scrape_polizei_brandenburg.py
```

## CLI-Parameter

- `--start-date`: optional, Format `YYYY-MM-DD`
- `--end-date`: optional, Format `YYYY-MM-DD`
- `--headless`: optional, startet den Browser unsichtbar
- `--max-pages`: optional, begrenzt die Anzahl der Suchseiten, z. B. für Tests
- `--allow-empty-overwrite`: optional, erlaubt leere Ausgabedateien bei 0 Treffern

## Beispielaufrufe

```bash
python src/data_collection/scrape_polizei_brandenburg.py
```

Vollständiger 3-Jahres-Lauf mit Resume:

- Ohne weitere Parameter starten.
- Wenn ein Lauf unterbrochen wird, denselben Befehl erneut ausführen.

```bash
python src/data_collection/scrape_polizei_brandenburg.py --start-date 2025-01-01 --end-date 2025-12-31
```

Testlauf mit wenigen Suchseiten:

```bash
python src/data_collection/scrape_polizei_brandenburg.py --max-pages 3
```

```bash
python src/data_collection/scrape_polizei_brandenburg.py --headless
```

## Ausgabedateien

### `data/raw/polizei_brandenburg_verkehr_raw.csv`

Enthält eine Zeile pro Pressemeldung mit folgenden Spalten:

- `veroeffentlichungsdatum`
- `titel`
- `ort`
- `kategorie`
- `tags`
- `text`
- `url`
- `traffic_match_reason`
- `traffic_match_type`
- `ereignisdatum_start`
- `ereignisdatum_ende`
- `ereignisdatum_text`
- `ereignisdatum_confidence`
- `event_date_plausible`
- `datum_fuer_modell`
- `datum_fuer_modell_quelle`

Bedeutung der Datumsfelder:

- `veroeffentlichungsdatum`: Datum der Veröffentlichung auf der Polizeiseite
- `ereignisdatum_start`: erkannter Start des tatsächlichen Ereignisses
- `ereignisdatum_ende`: erkannter Endzeitpunkt bei Zeiträumen, sonst meist identisch zu `ereignisdatum_start`
- `ereignisdatum_text`: die Textstelle, aus der das Ereignisdatum abgeleitet wurde
- `ereignisdatum_confidence`: `high`, `medium`, `low` oder `unknown`
- `datum_fuer_modell`: bevorzugt `ereignisdatum_start` bei `high`/`medium`, sonst `veroeffentlichungsdatum`
- `datum_fuer_modell_quelle`: `event_date` oder `publication_date`

### `data/processed/polizei_verkehr_daily.csv`

Enthält eine Tagesaggregation über den gewählten Zeitraum auf Basis von `datum_fuer_modell` mit folgenden Spalten:

- `datum`
- `anzahl_verkehrsmeldungen`
- `verkehrsmeldung_vorhanden`

Bedeutung der Aggregationsspalten:

- `anzahl_verkehrsmeldungen`: Anzahl der Verkehrsmeldungen pro Tag
- `verkehrsmeldung_vorhanden`: `1`, wenn an diesem Tag mindestens eine Verkehrsmeldung vorhanden war, sonst `0`
