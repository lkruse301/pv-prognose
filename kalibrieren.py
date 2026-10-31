#!/usr/bin/env python3
"""Kalibrierung: gleicht das Ertragsmodell mit echten Erträgen aus FusionSolar ab.

Liest einen Export der Tages- oder Monatserträge (CSV oder Excel), rechnet für dieselben
Tage den Ertrag mit dem Modell aus dem damaligen Wetter nach und schreibt pro Monat einen
Korrekturfaktor in config.json (Feld "calibration"). App und Morgenbericht nutzen ihn danach.

Aufruf:  python3 kalibrieren.py EXPORT … [--spalte "Spaltenname"] [--dry-run]
         EXPORT: eine oder mehrere CSV-/Excel-Dateien oder ein Ordner mit solchen Dateien
         --spalte   Name der Ertragsspalte, falls sie nicht automatisch erkannt wird
         --dry-run  nur auswerten, config.json nicht ändern
"""
import csv
import glob
import io
import json
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, datetime, timedelta

import morgenbericht as mb

HERE = os.path.dirname(os.path.abspath(__file__))
MONTHS = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
# Spaltenköpfe, die den PV-Ertrag bezeichnen (erste passende gewinnt) …
YIELD_HEADERS = ["pv-ertrag", "pv ertrag", "pv-yield", "pv yield", "ertrag", "yield", "erzeugung", "production"]
# … und solche, die trotz "Ertrag" etwas anderes meinen
EXCLUDE_HEADERS = ["einspeis", "export", "verbrauch", "consum", "netz", "grid", "speicher", "batter", "einnahme",
                   "revenue", "co2", "theoret"]
MIN_DAYS_FOR_MONTH = 20  # ab so vielen Tagen zählt ein Monat voll, darunter wird zum Gesamtfaktor gemischt


# ---------- Export lesen ----------
def read_xlsx(path):
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")))
        sheet = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet"))[0]
        rows = []
        for row in ET.fromstring(z.read(sheet)).iter(f"{{{ns['m']}}}row"):
            cells = {}
            for c in row.findall("m:c", ns):
                col = re.match(r"[A-Z]+", c.get("r")).group()
                idx = 0
                for ch in col:
                    idx = idx * 26 + ord(ch) - 64
                v = c.find("m:v", ns)
                text = "" if v is None else v.text or ""
                if c.get("t") == "s" and text:
                    text = shared[int(text)]
                elif c.get("t") == "inlineStr":
                    text = "".join(t.text or "" for t in c.iter(f"{{{ns['m']}}}t"))
                cells[idx - 1] = text
            rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
        return rows


def read_csv(path):
    raw = open(path, encoding="utf-8-sig", errors="replace").read()
    dialect = csv.Sniffer().sniff(raw[:4096], delimiters=";,\t")
    return list(csv.reader(io.StringIO(raw), dialect))


def parse_date(text):
    """Tag (date) oder Monat ((Jahr, Monat)) aus typischen Schreibweisen; None, wenn kein Datum."""
    t = str(text).strip()
    if re.fullmatch(r"\d{5}(\.0+)?", t):  # Excel-Seriennummer
        return date(1899, 12, 30) + timedelta(days=int(float(t)))
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(t[:len(datetime.now().strftime(fmt))], fmt).date()
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})", t) or re.fullmatch(r"(\d{1,2})[./](\d{4})", t)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return (a, b) if a > 31 else (b, a)
    return None


def parse_number(text):
    t = str(text).strip().replace(" ", "")
    if not t or t in ("-", "--"):
        return None
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") else t.replace(",", "")
    else:
        t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def read_yields(path, column=None):
    rows = read_xlsx(path) if path.lower().endswith((".xlsx", ".xlsm")) else read_csv(path)
    for h, header in enumerate(rows):
        names = [str(c).strip().lower() for c in header]
        if column:
            matches = [i for i, n in enumerate(names) if n == column.strip().lower()]
        else:
            matches = []
            for key in YIELD_HEADERS:
                matches = [i for i, n in enumerate(names) if key in n and not any(x in n for x in EXCLUDE_HEADERS)]
                if matches:
                    break
        if not matches:
            continue
        col = matches[0]
        data = rows[h + 1:]
        date_col = max(range(len(header)), key=lambda c: sum(1 for r in data if c < len(r) and parse_date(r[c])))
        unit = header[col]
        factor = 1 / 1000 if re.search(r"\bwh\b", unit.lower()) and "kwh" not in unit.lower() else 1
        out = {}
        for r in data:
            if max(col, date_col) >= len(r):
                continue
            d, v = parse_date(r[date_col]), parse_number(r[col])
            if d is not None and v is not None:
                out[d] = out.get(d, 0) + v * factor
        if out:
            return out, header[col]
    sys.exit("Keine Ertragsspalte gefunden. Gib den Spaltennamen mit --spalte \"…\" an.")


# ---------- Modell für vergangene Tage ----------
def model_daily(first, last):
    """Modellierter Tagesertrag (kWh) ohne Kalibrierung und Klarheit je Tag, aus dem Wetter dieser Tage."""
    # Historische Vorhersagen passen am besten zur Prognose; es gibt sie ab 2022.
    host = ("https://historical-forecast-api.open-meteo.com/v1/forecast" if first >= date(2022, 1, 1)
            else "https://archive-api.open-meteo.com/v1/archive")
    S = mb.S
    url = (f"{host}?latitude={S['lat']}&longitude={S['lon']}&tilt={S['tilt']}&azimuth={S['azimuth']}"
           f"&timezone=Europe%2FBerlin&start_date={first}&end_date={last}"
           "&hourly=global_tilted_irradiance,shortwave_radiation,diffuse_radiation,direct_normal_irradiance,"
           "temperature_2m,wind_speed_10m")
    h = json.loads(mb.curl([url]))["hourly"]
    saved, S["calibration"] = S.get("calibration"), [1] * 12
    days = defaultdict(float)
    try:
        for i, t in enumerate(h["time"]):
            if h["global_tilted_irradiance"][i] is None:
                continue
            start = datetime.fromisoformat(t) - timedelta(hours=1)
            days[start.date()] += mb.hourly_pv(h, i, start)
    finally:
        S["calibration"] = saved
    return days, mb.clarity_by_day(h)


# ---------- Wetterabhängige Korrektur und Bandbreite ----------
BAND_LIMITS = [0.35, 0.65]  # Klarheit: trüb < 0,35 ≤ wechselhaft < 0,65 ≤ sonnig


def quantile(values, q):
    v = sorted(values)
    x = (len(v) - 1) * q
    lo = int(x)
    return v[lo] + (v[min(lo + 1, len(v) - 1)] - v[lo]) * (x - lo)


def fit_weather(days, actual, model, clarity, factors):
    """Faktor a + b·Klarheit, der die Restabweichung nach den Monatsfaktoren erklärt (gewichtet nach Ertrag),
    und die 10–90-%-Spanne der Abweichung je Wetterlage."""
    base = {d: model[d] * factors[d.month - 1] for d in days}
    xs = [clarity.get(d, 0.5) for d in days]
    ys = [actual[d] / base[d] for d in days]
    ws = [base[d] for d in days]
    total = sum(ws)
    mx = sum(w * x for w, x in zip(ws, xs)) / total
    my = sum(w * y for w, y in zip(ws, ys)) / total
    b = sum(w * (x - mx) * (y - my) for w, x, y in zip(ws, xs, ys)) / sum(w * (x - mx) ** 2 for w, x in zip(ws, xs))
    a = my - b * mx
    pred = {d: base[d] * min(max(a + b * clarity.get(d, 0.5), 0.4), 1.3) for d in days}

    mae_before = sum(abs(base[d] - actual[d]) for d in days) / len(days)
    mae_after = sum(abs(pred[d] - actual[d]) for d in days) / len(days)
    print(f"\nWetterabhängige Korrektur: Faktor = {a:.3f} + {b:.3f} × Klarheit")
    print(f"Mittlere Tagesabweichung: {mae_before:.1f} kWh → {mae_after:.1f} kWh")

    groups = [[], [], []]
    for d in days:
        groups[sum(1 for limit in BAND_LIMITS if clarity.get(d, 0.5) >= limit)].append(actual[d] / pred[d])
    ranges = []
    print("Wetterlage      Tage  wahrscheinlicher Bereich (10–90 %)")
    for name, g in zip(("trüb", "wechselhaft", "sonnig"), groups):
        lo, hi = (quantile(g, 0.1), quantile(g, 0.9)) if len(g) >= 10 else (0.5, 1.5)
        ranges.append([round(lo, 2), round(hi, 2)])
        print(f"{name:<14}{len(g):>6}  {lo * 100:.0f} % – {hi * 100:.0f} % der Prognose")
    return {"a": round(a, 3), "b": round(b, 3)}, {"limits": BAND_LIMITS, "ranges": ranges}


# ---------- Auswertung ----------
def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    column = None
    if "--spalte" in args:
        column = args[args.index("--spalte") + 1]
    paths = [a for a in args if not a.startswith("--") and a != column]
    files = []
    for p in paths:
        if os.path.isdir(p):
            files += sorted(f for ext in ("*.xlsx", "*.xlsm", "*.csv") for f in glob.glob(os.path.join(p, ext)))
        else:
            files.append(p)
    if not files:
        sys.exit(__doc__)
    actual = {}
    for f in files:
        part, col_name = read_yields(f, column)
        actual.update(part)

    monthly_rows = all(isinstance(k, tuple) for k in actual)
    if monthly_rows:
        keys = sorted(actual)
        first, last = date(*keys[0], 1), date(*keys[-1], 28) + timedelta(days=4)
        last = last - timedelta(days=last.day)
    else:
        first, last = min(actual), max(actual)
    last = min(last, date.today() - timedelta(days=1))
    model, clarity = model_daily(first, last)

    sums = defaultdict(lambda: [0.0, 0.0, 0])  # Monat → [echt, Modell, Tage]
    used = []  # auswertbare Tage für Wetterkorrektur und Bandbreite
    skipped = 0
    if monthly_rows:
        for (y, m), kwh in actual.items():
            mod = sum(v for d, v in model.items() if d.year == y and d.month == m)
            if kwh > 0 and mod > 1:
                s = sums[m - 1]
                s[0] += kwh
                s[1] += mod
                s[2] += sum(1 for d in model if d.year == y and d.month == m)
    else:
        for d, kwh in actual.items():
            mod = model.get(d, 0)
            # Tage mit Schnee, Ausfall oder fehlenden Daten verzerren den Faktor
            if mod < 0.5 or kwh <= 0 or not 0.3 <= kwh / mod <= 2.5:
                skipped += 1
                continue
            s = sums[d.month - 1]
            s[0] += kwh
            s[1] += mod
            s[2] += 1
            used.append(d)

    total_actual = sum(s[0] for s in sums.values())
    total_model = sum(s[1] for s in sums.values())
    if total_model <= 0:
        sys.exit("Zu wenige auswertbare Tage für eine Kalibrierung.")
    overall = total_actual / total_model
    factors = []
    print(f"{len(files)} Datei(en) · Ertragsspalte: {col_name.strip()}  ·  Zeitraum {first:%d.%m.%Y}–{last:%d.%m.%Y}"
          + (f"  ·  {skipped} Tage übersprungen" if skipped else ""))
    print("Monat   echt kWh  Modell kWh  Tage  Faktor")
    for m in range(12):
        a, mod, n = sums.get(m, [0, 0, 0])
        if n:
            w = min(n / MIN_DAYS_FOR_MONTH, 1)
            f = w * (a / mod) + (1 - w) * overall
            print(f"{MONTHS[m]:<6}{a:>10.0f}{mod:>12.0f}{n:>6}  {f:.3f}")
        else:
            f = overall
        factors.append(round(min(max(f, 0.6), 1.3), 3))
    print(f"Gesamt  {total_actual:>8.0f}{total_model:>12.0f}        {overall:.3f}")
    print("Monate ohne Daten bekommen den Gesamtfaktor.")

    weather, band = None, None
    if len(used) >= 60:
        weather, band = fit_weather(used, actual, model, clarity, factors)

    if dry_run:
        print("Testlauf: config.json nicht geändert.")
        return
    path = os.path.join(HERE, "config.json")
    cfg = json.load(open(path, encoding="utf-8"))
    cfg["calibration"] = factors
    if weather:
        cfg["weatherCorrection"] = weather
        cfg["band"] = band
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("Kalibrierung in config.json gespeichert.")


if __name__ == "__main__":
    main()
