#!/usr/bin/env python3
"""Speichert die Prognose des heutigen Tages, damit die App die letzten zwei Tage noch anzeigen kann.

Läuft jeden Morgen per GitHub Actions (.github/workflows/prognose-speichern.yml).
Legt prognosen/JJJJ-MM-TT.json an und behält heute und die letzten zwei Tage; ältere Dateien
werden gelöscht. Eine schon vorhandene Tagesdatei bleibt unverändert, damit die Morgenprognose zählt.

Aufruf:  python3 prognose_speichern.py
"""
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import morgenbericht as mb

FOLDER = os.path.join(mb.HERE, "prognosen")
KEEP_PAST_DAYS = 2


def prune(today):
    """Löscht gespeicherte Prognosen, die älter als die letzten zwei Tage sind."""
    oldest = (today - timedelta(days=KEEP_PAST_DAYS)).strftime("%Y-%m-%d")
    for name in os.listdir(FOLDER):
        if name.endswith(".json") and name[:-5] < oldest:
            os.remove(os.path.join(FOLDER, name))
            print(f"Gelöscht: {name}")


def main():
    now = datetime.now(ZoneInfo("Europe/Berlin")).replace(tzinfo=None)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    os.makedirs(FOLDER, exist_ok=True)
    path = os.path.join(FOLDER, f"{midnight:%Y-%m-%d}.json")
    if os.path.exists(path):
        print(f"Schon gespeichert: {os.path.basename(path)}")
    else:
        # Ab 0 Uhr rechnen, damit der ganze Tag gespeichert wird, auch wenn der Lauf später startet
        report = mb.build_report(mb.fetch_forecast(now), midnight, midnight)
        report["createdAt"] = now.isoformat(timespec="minutes")
        for field in ("title", "message"):
            del report[field]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
            f.write("\n")
        print(f"Gespeichert: {os.path.basename(path)} · {report['pvKwh']} kWh · Quelle {report['source']}")
    prune(midnight)


if __name__ == "__main__":
    main()
