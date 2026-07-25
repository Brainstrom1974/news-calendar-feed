#!/usr/bin/env python3
"""
fetch_news_calendar.py  -  Version 2 (25.07.2026)

Zieht die High-Impact-Events der laufenden Woche von der jblanked.com
Calendar-API, schreibt sie in ein pipe-getrenntes Textformat und pusht die
Datei in ein GitHub-Repo (Raw-URL wird danach vom EA per WebRequest gelesen).

WICHTIG (Design-Grundsatz, siehe Gesamtplan 21.07.2026): Der EA liest NIE
Forex Factory oder jblanked.com direkt - nur diese eigene, von uns
kontrollierte Datei.

AENDERUNGEN GEGENUEBER VERSION 1 (alle drei am 25.07.2026 diagnostiziert):

  (1) CNY UND HKD FEHLTEN KOMPLETT. RELEVANT_CURRENCIES enthielt nur
      {USD, EUR, GBP, JPY}. Genau die beiden Waehrungen, die The5ers fuer
      HK50 als relevant nennt, wurden weggefiltert - der News-Filter haette
      fuer HK50 also nie etwas blockiert, egal wie viele Events es gab.

  (2) NUR EINE QUELLE. Die geplante Zwei-Quellen-Architektur (Forex Factory
      fuer EUR/GBP/JPY/USD, MQL5 fuer CNY/HKD) war nie umgesetzt. Der
      MQL5-Endpunkt lautet:
          https://www.jblanked.com/news/api/mql5/calendar/week/
      Beide Quellen folgen demselben Muster, nur die Quelle im Pfad
      unterscheidet sich.

  (3) VERGANGENE EVENTS. /calendar/week/ liefert die LAUFENDE Woche, nicht
      die kommende (der Docstring in Version 1 behauptete das Gegenteil).
      Am Wochenende liegt damit praktisch alles in der Vergangenheit. Neu
      werden abgelaufene Events verworfen, damit der Dateiinhalt sofort
      zeigt, ob der Abruf ueberhaupt Brauchbares geliefert hat.

Dateiformat der Ausgabe (unveraendert, der EA-Parser bleibt kompatibel):
    YYYY-MM-DD HH:MM|CCY|IMPACT|Event-Name
    Zeiten in GMT/UTC, damit der EA sie 1:1 mit TimeGMT() vergleichen kann.
"""

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
#   MQL5           -> HKD (Hongkong-lokale Daten; Forex Factory fuehrt HKD
#                     nicht, MQL5 nachweislich schon - GDP, Unemployment,
#                     Retail Sales, FX Reserves)
# CNY steht bewusst bei BEIDEN: Forex Factory fuehrt China-Daten, MQL5
# ebenfalls. Doppelte Eintraege werden unten dedupliziert.
SOURCES = {
    "forex-factory": {"USD", "EUR", "GBP", "JPY", "CNY"},
    "mql5":          {"CNY", "HKD"},
}

# GMT-Offset-Konvention laut jblanked-Doku: "GMT-3 = 0, GMT = 3, EST = 7,
# PST = 10". Wir wollen GMT/UTC in der Ausgabedatei -> offset=3.
JBLANKED_OFFSET_FOR_GMT = 3

# Wie weit zurueck ein Event noch geschrieben wird. Kleiner Puffer, damit ein
# Event, das gerade laeuft, nicht durch die Laufzeit des Skripts herausfaellt.
KEEP_PAST_MINUTES = 15

BASE_URL = "https://www.jblanked.com/news/api/{source}/calendar/week/"

# ---------------------------------------------------------------------


def fetch_week_events(source: str) -> list[dict]:
    """Holt die Events der laufenden Woche fuer eine Quelle (forex-factory | mql5).

    Der Impact-Filter wird bewusst NICHT als Server-Parameter gesetzt, sondern
    unten clientseitig angewendet - die Impact-Einstufung unterscheidet sich
    zwischen den Quellen, und wir wollen sehen, was wirklich zurueckkommt.
    """
    url = BASE_URL.format(source=source)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {JBLANKED_API_KEY}",
    }
    params = {"offset": JBLANKED_OFFSET_FOR_GMT}
    response = requests.get(url, headers=headers, params=params, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(
            f"jblanked.com Anfrage an '{source}' fehlgeschlagen: "
            f"HTTP {response.status_code} - {response.text[:300]}"
        )
    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError(f"Unerwartetes Antwortformat von '{source}': {type(data)}")
    return data


def parse_jblanked_date(date_str: str) -> datetime:
    """jblanked liefert Datum als 'YYYY.MM.DD HH:MM:SS'."""
    return datetime.strptime(date_str, "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc)


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
            raw = fetch_week_events(source)
        except Exception as exc:  # bewusst breit: Netzwerk, HTTP, JSON
            warnings.append(f"WARNUNG: Quelle '{source}' nicht abrufbar - {exc}")
            continue

        kept = 0
        skipped_past = 0
        for ev in raw:
            currency = str(ev.get("Currency", "")).upper()
            if currency not in wanted_currencies:
                continue
            if str(ev.get("Impact", "")).upper() != "HIGH":
                continue
            try:
                dt = parse_jblanked_date(ev["Date"])
            except (KeyError, ValueError, TypeError):
                continue  # unparsebare Zeile ueberspringen statt abbrechen
            if dt < cutoff:
                skipped_past += 1
                continue

            name = str(ev.get("Name", "")).replace("|", "-").strip()
            key = (dt.strftime("%Y-%m-%d %H:%M"), currency, name)
            if key in seen:
                continue  # CNY kann aus beiden Quellen kommen
            seen.add(key)
            events.append((dt, currency, name))
            kept += 1

        print(f"Quelle '{source}': {kept} kuenftige High-Impact-Events uebernommen "
              f"(von {len(raw)} insgesamt, {skipped_past} bereits vergangen).")
        if kept == 0:
            warnings.append(
                f"WARNUNG: Quelle '{source}' lieferte 0 kuenftige High-Impact-Events "
                f"fuer {sorted(wanted_currencies)}. Entweder gibt es diese Woche "
                f"wirklich keine, oder der Abruf stimmt nicht."
            )

    events.sort(key=lambda x: x[0])
    return events, warnings


def build_output_lines(events: list[tuple[datetime, str, str]], warnings: list[str]) -> list[str]:
    lines = [
        "# Automatisch generiert von fetch_news_calendar.py - NICHT manuell editieren",
        f"# Generiert: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "# Format: YYYY-MM-DD HH:MM|CCY|IMPACT|Event-Name (Zeit in GMT/UTC)",
        f"# Quellen: {', '.join(SOURCES)} | nur kuenftige Events",
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
    print(f"Gesamt: {len(events)} Events in die Datei geschrieben.")
    write_and_publish(build_output_lines(events, warnings))


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------
# BEKANNTE EINSCHRAENKUNG
#
# /calendar/week/ liefert nur die LAUFENDE Woche. Gegen Wochenende schrumpft
# die Vorschau daher zwangslaeufig zusammen, im Extremfall auf null Zeilen.
# Das ist unkritisch, solange der Workflow taeglich laeuft: Montag frueh
# fuellt sich die Datei wieder fuer die ganze Woche. Ein "next week"-Endpunkt
# existiert bei jblanked laut Doku nicht.
#
# Rate-Limit laut Doku: eine Anfrage pro Sekunde. Zwei Abrufe pro Tag sind
# damit unproblematisch - die urspruengliche Sorge "1 Request pro Tag" war
# ein Missverstaendnis.
# ---------------------------------------------------------------------
