#!/usr/bin/env python3
"""
fetch_news_calendar.py

Zieht einmal taeglich die kommende Woche der High-Impact Forex-Factory-Events
von der jblanked.com Calendar-API, schreibt sie in ein einfaches, pipe-
getrenntes Textformat und pusht die Datei in ein GitHub-Repo (Raw-URL wird
danach vom EA per WebRequest gelesen).

WICHTIG (siehe Pendenzen/Gesamtplan 21.07.2026): Der EA liest NIE Forex
Factory oder jblanked.com direkt - nur diese eigene, von uns kontrollierte
Datei. Bitte diesen Design-Grundsatz beibehalten.

Voraussetzungen:
    pip install requests
    Ein GitHub Personal Access Token mit "repo"-Scope (fuer privates Repo)
    oder gar keins noetig, falls das Repo public ist und der Push ueber
    einen bereits lokal eingerichteten "git" mit SSH/HTTPS-Credentials laeuft.

Empfohlener Ablauf, ohne extra Server:
    - Dieses Skript liegt in einem lokal geklonten Git-Repo (z.B.
      ~/news-calendar-feed/).
    - Einmal taeglich per cron/launchd ausgefuehrt (siehe Hinweis unten).
    - Das Skript schreibt news_events.txt, macht "git add/commit/push".

Dateiformat der Ausgabe (siehe EA-Kommentar im News-Filter-Modul):
    YYYY-MM-DD HH:MM|CCY|IMPACT|Event-Name
    Zeiten in GMT/UTC (jblanked-Offset bewusst auf GMT gesetzt, siehe
    OFFSET_GMT unten), damit der EA sie 1:1 mit TimeGMT() vergleichen kann,
    ohne noch eine weitere Zeitzonen-Umrechnung im MQL5-Code zu brauchen -
    genau die Art Fehlerquelle, die uns bei Broker-Zeit/Zuerich-Zeit schon
    mehrfach Probleme gemacht hat.
"""

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------
# KONFIGURATION - bitte anpassen
# ---------------------------------------------------------------------
JBLANKED_API_KEY = os.environ.get("JBLANKED_API_KEY", "DEIN_API_KEY_HIER")  # lokal: hier eintragen. In GitHub Actions: als Repository Secret gesetzt, siehe Workflow-Datei.
REPO_LOCAL_PATH = Path(".") if os.environ.get("GITHUB_ACTIONS") else Path.home() / "news-calendar-feed"   # lokal: eigener Clone. In GitHub Actions: aktuelles Verzeichnis (Workflow checkt das Repo bereits selbst aus)
OUTPUT_FILENAME = "news_events.txt"
GIT_COMMIT_MESSAGE_PREFIX = "Update news calendar"

# Nur diese Waehrungen werden ueberhaupt gespeichert (deckt alle fuenf
# Instrumente ab: EUR=GER40, USD=NASDAQ/HK50/alle Zusatz-USD-Sperren,
# GBP=FTSE100, JPY=JPN225). Kleiner als noetig zu filtern spart Zeilen,
# aber Vorsicht: falls spaeter weitere Instrumente/Waehrungen dazukommen,
# hier ergaenzen.
RELEVANT_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CNY", "HKD"}

# GMT-Offset-Konvention laut jblanked-Doku: "GMT-3 = 0, GMT = 3, EST = 7,
# PST = 10". Wir wollen GMT/UTC in der Ausgabedatei -> offset=3.
JBLANKED_OFFSET_FOR_GMT = 3

# ---------------------------------------------------------------------

JBLANKED_URL_FOREX_FACTORY = "https://www.jblanked.com/news/api/forex-factory/calendar/week/"
# The5ers-Support (21.07.2026) verweist fuer HK50 explizit auf CNY/CNH-Events.
# Forex Factory deckt China vermutlich duenner ab als MQL5's Kalender (siehe
# EA-Kommentar im News-Filter-Modul) - deshalb zusaetzlicher Abruf ueber die
# MQL5-Quelle, NUR fuer CNY gefiltert, um die Forex-Factory-basierte Logik
# fuer die anderen vier Instrumente nicht zu vermischen/verwaessern.
JBLANKED_URL_MQL5 = "https://www.jblanked.com/news/api/mql5/calendar/week/"


def fetch_week_events(url: str, currency_filter: str | None = None) -> list[dict]:
    """Holt die kommende Woche an Events von der angegebenen jblanked-Quelle.
    currency_filter optional (z.B. 'CNY'), um bei der MQL5-Quelle direkt nur
    das zu holen, was wir brauchen, statt lokal aus einer grossen Liste zu
    filtern."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {JBLANKED_API_KEY}",
    }
    params = {
        "impact": "High",
        "offset": JBLANKED_OFFSET_FOR_GMT,
    }
    if currency_filter:
        params["currency"] = currency_filter

    response = requests.get(url, headers=headers, params=params, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(
            f"jblanked.com Anfrage fehlgeschlagen ({url}): HTTP {response.status_code} - {response.text[:300]}"
        )
    return response.json()


def parse_jblanked_date(date_str: str) -> datetime:
    """jblanked liefert Datum als 'YYYY.MM.DD HH:MM:SS' (siehe Doku-Beispiel)."""
    return datetime.strptime(date_str, "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc)


def build_output_lines(events: list[dict]) -> list[str]:
    lines = [
        "# Automatisch generiert von fetch_news_calendar.py - NICHT manuell editieren",
        f"# Generiert: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "# Format: YYYY-MM-DD HH:MM|CCY|IMPACT|Event-Name (Zeit in GMT/UTC)",
    ]
    kept = 0
    for ev in events:
        currency = ev.get("Currency", "")
        if currency not in RELEVANT_CURRENCIES:
            continue
        impact = ev.get("Impact", "").upper()
        if impact != "HIGH":
            continue
        try:
            dt = parse_jblanked_date(ev["Date"])
        except (KeyError, ValueError):
            continue  # unparsebare Zeile lieber ueberspringen als das ganze Skript abbrechen lassen
        name = ev.get("Name", "").replace("|", "-")  # Pipe im Namen wuerde das Format brechen
        lines.append(f"{dt.strftime('%Y-%m-%d %H:%M')}|{currency}|HIGH|{name}")
        kept += 1

    print(f"{kept} relevante High-Impact-Events uebernommen (von {len(events)} insgesamt).")
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
    # WICHTIG (Free-Tier-Limit 1 Request/Tag laut jblanked-Doku, Stand
    # 21.07.2026): Zwei Abrufe pro Tag (Forex Factory + MQL5) koennten das
    # Limit ueberschreiten, falls es sich strikt auf "insgesamt 1/Tag" statt
    # "1/Tag/Endpoint" bezieht. Beim ersten produktiven Lauf beide Antworten
    # pruefen (HTTP-Code, Log); falls der zweite Abruf mit 429/Rate-Limit
    # fehlschlaegt, ggf. VIP-Mitgliedschaft noetig (jblanked.com/api/billing/)
    # oder die beiden Abrufe zeitlich verteilen.
    #
    # MQL5-Abruf bewusst OHNE Server-seitigen currency-Filter (deckt sowohl
    # CNY als auch HKD in einer einzigen Anfrage ab statt zwei) - die
    # Auswahl passiert stattdessen client-seitig ueber RELEVANT_CURRENCIES
    # in build_output_lines().
    forex_factory_events = fetch_week_events(JBLANKED_URL_FOREX_FACTORY)
    try:
        china_hk_events = fetch_week_events(JBLANKED_URL_MQL5)
    except RuntimeError as exc:
        print(f"WARNUNG: MQL5-Abruf (CNY/HKD fuer HK50) fehlgeschlagen, HK50-Filter laeuft ggf. mit veralteten Daten weiter: {exc}", file=sys.stderr)
        china_hk_events = []

    all_events = forex_factory_events + china_hk_events
    lines = build_output_lines(all_events)
    write_and_publish(lines)


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------
# Einrichtung als taeglicher Cronjob (macOS/Linux), z.B. 06:00 Zuerich-Zeit:
#
#   crontab -e
#   0 6 * * * /usr/bin/python3 /pfad/zu/fetch_news_calendar.py >> /pfad/zu/fetch_news.log 2>&1
#
# Alternative macOS: launchd (ueberlebt Neustarts zuverlaessiger als cron),
# bei Bedarf sag Bescheid, dann bauen wir die .plist-Datei dazu.
# ---------------------------------------------------------------------
