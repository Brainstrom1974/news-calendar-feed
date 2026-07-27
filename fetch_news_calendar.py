
#!/usr/bin/env python3
"""
fetch_news_calendar.py  -  Version 4 (27.07.2026)

Zieht High-Impact-Events von der jblanked.com Calendar-API, schreibt sie in
ein pipe-getrenntes Textformat und pusht die Datei in ein GitHub-Repo
(Raw-URL wird danach vom EA per WebRequest gelesen).

WICHTIG (Design-Grundsatz, siehe Gesamtplan 21.07.2026): Der EA liest NIE
Forex Factory oder jblanked.com direkt - nur diese eigene, von uns
kontrollierte Datei.

AENDERUNGEN GEGENUEBER VERSION 3 (27.07.2026, gleicher Tag):

  (0) ENDPUNKT-VARIANTEN WERDEN DURCHPROBIERT. Version 3 lieferte fuer
      BEIDE Quellen exakt dieselben zwei Datensaetze ("SPPI y/y", JPY,
      Impact "None", Event_ID 0) - obwohl forex-factory und mql5 voellig
      verschiedene Kalender sind und in dieser Woche u.a. der FED-Zins-
      entscheid ansteht. Die Antwort ist also nachweislich falsch, nicht
      nur leer. Verdacht: der Parameter "offset" wird vom range-Endpunkt
      nicht als Zeitzone (so die Doku), sondern als Paginierung gedeutet.
      Statt zu raten probiert das Skript nun mehrere Endpunkt-/Parameter-
      Kombinationen durch, protokolliert je Variante HTTP-Status, Anzahl
      Rohdatensaetze und die vorkommenden Impact-Werte, und verwendet
      danach die ergiebigste. Sobald eine Variante genug brauchbare Daten
      liefert, wird abgebrochen - das spart Credits.

AENDERUNGEN GEGENUEBER VERSION 2 (27.07.2026):

  (1) RANGE-ENDPUNKT STATT WOCHE. /calendar/week/ liefert nur die LAUFENDE
      Woche. Dadurch war die Datei sonntags zwangslaeufig leer und musste
      Montag frueh neu gefuellt werden - ausgerechnet dann, wenn die
      GitHub-Action nachweislich sechs bis siebeneinhalb Stunden zu spaet
      kommt (gemessen an den Laeufen #3/#5/#6/#9 vom 23.-26.07.2026).
      Neu wird /calendar/range/?from=&to= verwendet und ein Fenster von
      RANGE_DAYS Tagen ab heute geholt. Damit stehen die Termine der
      naechsten Woche schon Tage im Voraus in der Datei und die genaue
      Laufzeit der Action ist gleichgueltig. Faellt der range-Abruf aus,
      wird automatisch auf /calendar/week/ zurueckgefallen.

  (2) DIAGNOSE-AUSGABE. Am 27.07.2026 lieferte forex-factory 2 Eintraege
      und mql5 null, ohne dass erkennbar war warum - das Skript verwarf
      alles stumm. Neu wird pro Quelle protokolliert: HTTP-Status, Anzahl
      Rohdaten, alle vorkommenden Waehrungen, alle vorkommenden
      Impact-Werte, die ersten Rohdatensaetze im Original sowie eine
      Aufschluesselung, an welchem Filter die Eintraege ausscheiden
      (Waehrung / Impact / Datumsformat / bereits vergangen).

  (3) CNH ERGAENZT. Der EA filtert seit v1.25 auf CNY, CNH, HKD und USD.
      Das Skript fragte CNH nie ab - CNH-Events konnten also gar nie in
      der Datei landen.

Dateiformat der Ausgabe (unveraendert, der EA-Parser bleibt kompatibel):
    YYYY-MM-DD HH:MM|CCY|IMPACT|Event-Name
    Zeiten in GMT/UTC, damit der EA sie 1:1 mit TimeGMT() vergleichen kann.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------
# KONFIGURATION
# ---------------------------------------------------------------------
JBLANKED_API_KEY = os.environ.get("JBLANKED_API_KEY", "DEIN_API_KEY_HIER")
REPO_LOCAL_PATH = Path(".") if os.environ.get("GITHUB_ACTIONS") else Path.home() / "news-calendar-feed"
OUTPUT_FILENAME = "news_events.txt"
GIT_COMMIT_MESSAGE_PREFIX = "Update news calendar"

# Zwei Quellen, weil keine allein alles abdeckt:
#   Forex Factory  -> EUR (GER40), GBP (FTSE100), JPY (JPN225), USD (NASDAQ
#                     und optionale USD-Zusatzsperre), CNY (China/Hang Seng)
#   MQL5           -> HKD und CNH (Hongkong-lokale Daten; Forex Factory
#                     fuehrt HKD nicht, MQL5 nachweislich schon - GDP,
#                     Unemployment, Retail Sales, FX Reserves)
# CNY steht bewusst bei BEIDEN. Doppelte Eintraege werden dedupliziert.
SOURCES = {
    "forex-factory": {"USD", "EUR", "GBP", "JPY", "CNY"},
    "mql5":          {"CNY", "CNH", "HKD"},
}

# GMT-Offset-Konvention laut jblanked-Doku: "GMT-3 = 0, GMT = 3, EST = 7,
# PST = 10". Wir wollen GMT/UTC in der Ausgabedatei -> offset=3.
# ACHTUNG: Ob der range-Endpunkt diesen Parameter wirklich so versteht, ist
# unbestaetigt - siehe Aenderung (0) oben. Deshalb wird er unten auch in
# einer Variante ohne offset probiert.
JBLANKED_OFFSET_FOR_GMT = 3

# Ab so vielen Rohdatensaetzen gilt eine Variante als brauchbar; die
# restlichen Varianten werden dann nicht mehr abgerufen (spart Credits).
MIN_USABLE_RAW = 10

# Wie weit zurueck ein Event noch geschrieben wird. Kleiner Puffer, damit ein
# Event, das gerade laeuft, nicht durch die Laufzeit des Skripts herausfaellt.
KEEP_PAST_MINUTES = 15

# Wie viele Tage im Voraus geholt werden. 10 Tage decken die laufende UND
# die komplette naechste Woche ab - damit ist der Wochenwechsel entschaerft.
RANGE_DAYS = 10

RANGE_URL = "https://www.jblanked.com/news/api/{source}/calendar/range/"
WEEK_URL = "https://www.jblanked.com/news/api/{source}/calendar/week/"
TODAY_URL = "https://www.jblanked.com/news/api/{source}/calendar/today/"

# Diagnose-Ausgabe im Actions-Log. Kostet keine zusaetzlichen API-Credits,
# sondern protokolliert nur, was ohnehin abgerufen wurde.
DIAGNOSE = True
DIAGNOSE_SAMPLE_COUNT = 3

# ---------------------------------------------------------------------


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {JBLANKED_API_KEY}",
    }


def _get(url: str, params: dict, label: str) -> list[dict]:
    """Einzelner GET-Aufruf mit Protokollierung von Status und Rohumfang."""
    response = requests.get(url, headers=_headers(), params=params, timeout=30)
    print(f"[DIAG] {label}: HTTP {response.status_code} <- {response.url}")
    if response.status_code != 200:
        raise RuntimeError(
            f"jblanked.com Anfrage '{label}' fehlgeschlagen: "
            f"HTTP {response.status_code} - {response.text[:300]}"
        )
    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError(
            f"Unerwartetes Antwortformat bei '{label}': {type(data)} - "
            f"{str(data)[:300]}"
        )
    return data


def _variants(source: str) -> list[tuple[str, str, dict]]:
    """Zu probierende Endpunkt-/Parameter-Kombinationen, ergiebigste zuerst."""
    today = datetime.now(timezone.utc).date()
    until = today + timedelta(days=RANGE_DAYS)
    frm, to = today.strftime("%Y-%m-%d"), until.strftime("%Y-%m-%d")
    return [
        ("range ohne offset", RANGE_URL.format(source=source), {"from": frm, "to": to}),
        ("range mit offset",  RANGE_URL.format(source=source), {"from": frm, "to": to, "offset": JBLANKED_OFFSET_FOR_GMT}),
        ("week ohne offset",  WEEK_URL.format(source=source),  {}),
        ("week mit offset",   WEEK_URL.format(source=source),  {"offset": JBLANKED_OFFSET_FOR_GMT}),
        ("today",             TODAY_URL.format(source=source), {}),
    ]


def fetch_events(source: str) -> list[dict]:
    """Probiert die Varianten durch und gibt die ergiebigste Antwort zurueck.

    Der Impact-Filter wird bewusst NICHT als Server-Parameter gesetzt,
    sondern unten clientseitig angewendet: Die Impact-Einstufung
    unterscheidet sich zwischen den Quellen, und wir wollen im Log sehen,
    was wirklich zurueckkommt.
    """
    best_label, best_data = None, []

    for label, url, params in _variants(source):
        try:
            data = _get(url, params, f"{source} [{label}]")
        except Exception as exc:
            print(f"[DIAG] {source} [{label}]: nicht nutzbar - {exc}")
            continue

        impacts = sorted({str(ev.get("Impact", "<fehlt>")) for ev in data}) if data else []
        high = sum(1 for ev in data if str(ev.get("Impact", "")).upper() == "HIGH")
        print(f"[DIAG] {source} [{label}]: {len(data)} Rohdatensaetze, "
              f"davon {high} mit Impact HIGH, Impact-Werte {impacts}")

        if len(data) > len(best_data):
            best_label, best_data = label, data

        if len(data) >= MIN_USABLE_RAW and high > 0:
            print(f"[DIAG] {source}: Variante '{label}' ist brauchbar - "
                  f"restliche Varianten werden uebersprungen.")
            return data

    if best_label is None:
        raise RuntimeError(f"Keine Variante lieferte eine verwertbare Antwort fuer '{source}'.")

    print(f"[DIAG] {source}: keine Variante war eindeutig brauchbar - "
          f"verwende die ergiebigste ('{best_label}', {len(best_data)} Datensaetze).")
    return best_data


def describe_raw(source: str, raw: list[dict]) -> None:
    """Protokolliert, was die Quelle tatsaechlich geliefert hat."""
    if not DIAGNOSE:
        return
    print(f"[DIAG] {source}: {len(raw)} Rohdatensaetze")
    if not raw:
        return
    currencies = sorted({str(ev.get("Currency", "<fehlt>")) for ev in raw})
    impacts = sorted({str(ev.get("Impact", "<fehlt>")) for ev in raw})
    keys = sorted({k for ev in raw[:20] for k in ev.keys()})
    print(f"[DIAG] {source}: Waehrungen im Rohdatensatz = {currencies}")
    print(f"[DIAG] {source}: Impact-Werte im Rohdatensatz = {impacts}")
    print(f"[DIAG] {source}: Feldnamen = {keys}")
    for i, ev in enumerate(raw[:DIAGNOSE_SAMPLE_COUNT], start=1):
        print(f"[DIAG] {source}: Beispiel {i} = {json.dumps(ev, ensure_ascii=False)[:400]}")


def parse_jblanked_date(date_str: str) -> datetime:
    """Beispielformat laut jblanked-Doku: '2025.04.11 12:30:00' (UTC bei offset=3)."""
    text = str(date_str).strip()
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unbekanntes Datumsformat: {date_str!r}")


def collect_events() -> tuple[list[tuple[datetime, str, str]], list[str]]:
    """Ruft alle Quellen ab und gibt (Events, Warnungen) zurueck.

    Ein Fehlschlag einer einzelnen Quelle bricht NICHT das ganze Skript ab -
    sonst wuerde ein Ausfall der MQL5-Quelle auch die EUR/GBP/JPY-Events
    kosten. Stattdessen wird gewarnt und mit dem Rest weitergemacht.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=KEEP_PAST_MINUTES)
    seen: set[tuple[str, str, str]] = set()
    events: list[tuple[datetime, str, str]] = []
    warnings: list[str] = []

    for source, wanted_currencies in SOURCES.items():
        try:
            raw = fetch_events(source)
        except Exception as exc:  # bewusst breit: Netzwerk, HTTP, JSON
            warnings.append(f"WARNUNG: Quelle '{source}' nicht abrufbar - {exc}")
            continue

        describe_raw(source, raw)

        kept = 0
        skip_currency = 0
        skip_impact = 0
        skip_date = 0
        skip_past = 0
        skip_dupe = 0
        bad_date_samples: list[str] = []

        for ev in raw:
            currency = str(ev.get("Currency", "")).upper()
            if currency not in wanted_currencies:
                skip_currency += 1
                continue
            if str(ev.get("Impact", "")).upper() != "HIGH":
                skip_impact += 1
                continue
            try:
                dt = parse_jblanked_date(ev["Date"])
            except (KeyError, ValueError, TypeError):
                skip_date += 1
                if len(bad_date_samples) < 3:
                    bad_date_samples.append(repr(ev.get("Date")))
                continue
            if dt < cutoff:
                skip_past += 1
                continue

            name = str(ev.get("Name", "")).replace("|", "-").strip()
            key = (dt.strftime("%Y-%m-%d %H:%M"), currency, name)
            if key in seen:
                skip_dupe += 1
                continue
            seen.add(key)
            events.append((dt, currency, name))
            kept += 1

        print(
            f"Quelle '{source}': {kept} kuenftige High-Impact-Events uebernommen "
            f"(von {len(raw)} insgesamt)."
        )
        print(
            f"[DIAG] {source}: aussortiert -> Waehrung {skip_currency}, "
            f"Impact {skip_impact}, Datumsformat {skip_date}, "
            f"vergangen {skip_past}, Duplikat {skip_dupe}"
        )
        if bad_date_samples:
            print(f"[DIAG] {source}: unlesbare Datumswerte = {bad_date_samples}")

        if kept == 0:
            warnings.append(
                f"WARNUNG: Quelle '{source}' lieferte 0 kuenftige High-Impact-Events "
                f"fuer {sorted(wanted_currencies)} (von {len(raw)} Rohdatensaetzen). "
                f"Aussortiert: Waehrung {skip_currency}, Impact {skip_impact}, "
                f"Datumsformat {skip_date}, vergangen {skip_past}."
            )

    events.sort(key=lambda x: x[0])
    return events, warnings


def build_output_lines(events: list[tuple[datetime, str, str]], warnings: list[str]) -> list[str]:
    lines = [
        "# Automatisch generiert von fetch_news_calendar.py - NICHT manuell editieren",
        f"# Generiert: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "# Format: YYYY-MM-DD HH:MM|CCY|IMPACT|Event-Name (Zeit in GMT/UTC)",
        f"# Quellen: {', '.join(SOURCES)} | Fenster: {RANGE_DAYS} Tage | nur kuenftige Events",
    ]
    for warning in warnings:
        lines.append(f"# {warning}")
    for dt, currency, name in events:
        lines.append(f"{dt.strftime('%Y-%m-%d %H:%M')}|{currency}|HIGH|{name}")
    return lines


def write_and_publish(lines: list[str]) -> None:
    REPO_LOCAL_PATH.mkdir(parents=True, exist_ok=True)
    output_path = REPO_LOCAL_PATH / OUTPUT_FILENAME
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Geschrieben: {output_path}")

    def run_git(*args: str) -> None:
        result = subprocess.run(
            ["git", *args], cwd=REPO_LOCAL_PATH, capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"git {' '.join(args)} -> {result.stdout}\n{result.stderr}", file=sys.stderr)

    run_git("add", OUTPUT_FILENAME)
    commit_msg = f"{GIT_COMMIT_MESSAGE_PREFIX} {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    run_git("commit", "-m", commit_msg)
    run_git("push")
    print("Git push abgeschlossen (oder 'nothing to commit', falls sich nichts geaendert hat).")


def main() -> None:
    events, warnings = collect_events()
    for warning in warnings:
        print(warning, file=sys.stderr)

    # SCHUTZ (26.07.2026): Eine leere Ergebnisliste darf eine bestehende, gefuellte
    # Datei NICHT ueberschreiben. Sonst loescht ein voruebergehender API-Ausfall
    # die Termine, die der EA braucht. Lieber eine leicht veraltete Datei behalten
    # als gar keine: Der EA verwirft abgelaufene Events ohnehin selbst, eine leere
    # Datei dagegen bedeutet "nie sperren".
    if not events:
        output_path = REPO_LOCAL_PATH / OUTPUT_FILENAME
        if output_path.exists() and output_path.stat().st_size > 0:
            print(
                "HINWEIS: 0 kuenftige Events ermittelt - bestehende "
                f"{OUTPUT_FILENAME} bleibt unveraendert erhalten (kein Ueberschreiben, "
                "kein Commit). Die [DIAG]-Zeilen oben zeigen, woran es lag."
            )
            return
        print(
            f"HINWEIS: 0 kuenftige Events und keine bestehende {OUTPUT_FILENAME} - "
            "es wird eine Datei nur mit Kopfzeilen angelegt."
        )

    print(f"Gesamt: {len(events)} Events in die Datei geschrieben.")
    write_and_publish(build_output_lines(events, warnings))


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------
# HINWEISE
#
# Credits: Ein Abruf kostet 1 Credit. Solange die Varianten-Probe aktiv ist,
# kostet ein Lauf bis zu fuenf Abrufe je Quelle, also hoechstens zehn - in
# der Regel weniger, weil nach der ersten brauchbaren Variante abgebrochen
# wird. Sobald klar ist, welche Variante funktioniert, sollte die Liste in
# _variants() auf genau diese eine gekuerzt werden (dann wieder 2 pro Lauf).
#
# GitHub-Actions-Zeitplan: Geplante Laeufe wurden am 23.-26.07.2026
# nachweislich um sechs bis siebeneinhalb Stunden verzoegert ausgefuehrt.
# Deshalb darf die Konstruktion nicht von einer genauen Uhrzeit abhaengen -
# das Fenster von RANGE_DAYS Tagen sorgt dafuer, dass ein verspaeteter oder
# ausgefallener Lauf folgenlos bleibt.
# ---------------------------------------------------------------------
