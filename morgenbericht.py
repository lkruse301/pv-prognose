#!/usr/bin/env python3
"""Morgenbericht: PV-Ertrag heute, Anteil fürs E-Auto und Ladestunden.

Läuft jeden Morgen in der Claude-Cloud-Routine „PV-Morgenbericht“, die den Text
als Push-Benachrichtigung aufs Handy schickt. Rechnet mit demselben Modell wie
js/pv.js und denselben Anlagenwerten (config.json).

Aufruf:  python3 morgenbericht.py [JJJJ-MM-TT]   (ohne Datum: heute)
Ausgabe: erste Zeile = Titel der Benachrichtigung, danach der Text.
"""
import json
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
REAR_SHADING = 0.6
FAIMAN_U0, FAIMAN_U1 = 25, 6.84
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
WEATHER = {
    0: "Klar", 1: "Überwiegend klar", 2: "Teilweise bewölkt", 3: "Bedeckt", 45: "Nebel", 48: "Reifnebel",
    51: "Leichter Niesel", 53: "Niesel", 55: "Starker Niesel", 56: "Gefrierender Niesel", 57: "Gefrierender Niesel",
    61: "Leichter Regen", 63: "Regen", 65: "Starker Regen", 66: "Gefrierender Regen", 67: "Gefrierender Regen",
    71: "Leichter Schnee", 73: "Schnee", 75: "Starker Schnee", 77: "Schneegriesel",
    80: "Leichte Schauer", 81: "Schauer", 82: "Starke Schauer", 85: "Schneeschauer", 86: "Starke Schneeschauer",
    95: "Gewitter", 96: "Gewitter mit Hagel", 99: "Gewitter mit Hagel",
}

with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
    S = json.load(f)


def ac_power_kw(gti, ghi, dhi, temp_c, wind_kmh):
    if not gti or gti <= 0:
        return 0.0
    beta = math.radians(S["tilt"])
    rear = 0.0
    if S["bifacial"]:
        rear = (S["albedo"] * ghi * (1 + math.cos(beta)) / 2 + dhi * (1 - math.cos(beta)) / 2) * REAR_SHADING
    geff = gti + S["bifaciality"] * max(rear, 0)
    wind = (wind_kmh if wind_kmh is not None else 5.4) / 3.6
    t_cell = temp_c + geff / (FAIMAN_U0 + FAIMAN_U1 * wind)
    p_dc = S["kwp"] * geff / 1000 * (1 + S["tempCoeff"] / 100 * (t_cell - 25))
    return max(0.0, min(p_dc * (1 - S["losses"] / 100), S["inverterKw"]))


def car_from(surplus):
    if surplus >= S["wallboxMinKw"]:
        return min(surplus, S["wallboxMaxKw"])
    if S["phaseSwitching"] and surplus >= S["wallboxMin1pKw"]:
        return min(surplus, S["wallboxMax1pKw"])
    return 0.0


def simulate_hour(state, pv):
    """Wie simulateHour in js/pv.js: Hauslast, dann Hausspeicher, dann Auto."""
    load = S["baseLoadKwhDay"] / 24
    cap = S["batteryKwh"]
    min_soc = cap * S["batteryMinSoc"] / 100
    net = pv - load
    car = 0.0
    if net > 0 and S["carFirst"]:
        car = car_from(net)
        net -= car
    if net > 0:
        charge = min(net, S["batteryMaxKw"], max(cap - state["soc"], 0))
        state["soc"] += charge
        net -= charge
        if not S["carFirst"]:
            car = car_from(net)
    elif net < 0:
        discharge = min(-net, S["batteryMaxKw"], max(state["soc"] - min_soc, 0))
        state["soc"] -= discharge
    return car


def car_percent(kwh):
    return kwh * (1 - S["chargeLossPct"] / 100) / S["carBatteryKwh"] * 100


def fmt(x, d=1):
    return f"{x:.{d}f}".replace(".", ",")


def curl(args):
    # curl statt urllib: die python.org-Installation auf macOS hat oft keine Zertifikate eingerichtet
    out = subprocess.run(["curl", "-sS", "--max-time", "30", "-w", "\n%{http_code}", *args],
                         capture_output=True, text=True)
    body, _, status = out.stdout.rpartition("\n")
    if out.returncode != 0 or status != "200":
        # Statuscode und Anfang der Antwort zeigen, ob der Wetterdienst oder ein Proxy ablehnt
        sys.exit(f"Abruf fehlgeschlagen: curl-Code {out.returncode}, HTTP {status or '-'}\n"
                 f"{out.stderr.strip()}\n{body[:300]}")
    return body


def fetch_forecast():
    url = (
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={S['lat']}&longitude={S['lon']}&tilt={S['tilt']}&azimuth={S['azimuth']}"
        "&timezone=Europe%2FBerlin&forecast_days=2"
        "&hourly=global_tilted_irradiance,shortwave_radiation,diffuse_radiation,temperature_2m,wind_speed_10m"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,sunshine_duration"
    )
    return json.loads(curl([url]))


def build_report(data, now, day):
    h = data["hourly"]
    key = day.strftime("%Y-%m-%d")
    state = {"soc": S["batteryKwh"] * S["batterySoc"] / 100}
    hours = []
    for i, t in enumerate(h["time"]):
        # Zeitstempel = Mittel der Stunde davor (11:00 → 10–11 Uhr)
        start = datetime.fromisoformat(t) - timedelta(hours=1)
        if start + timedelta(hours=1) <= now.replace(minute=0, second=0, microsecond=0):
            continue
        pv = ac_power_kw(h["global_tilted_irradiance"][i], h["shortwave_radiation"][i],
                         h["diffuse_radiation"][i], h["temperature_2m"][i], h["wind_speed_10m"][i])
        car = simulate_hour(state, pv)
        if start.strftime("%Y-%m-%d") == key:
            hours.append({"hour": start.hour, "pv": pv, "car": car})

    d = data["daily"]
    i = d["time"].index(key)
    pv_total = sum(x["pv"] for x in hours)
    car_total = sum(x["car"] for x in hours)
    car_hours = [x for x in hours if x["car"] > 0]
    label = f"{WEEKDAYS[day.weekday()]} {day.day}.{day.month}."
    weather = (f"{WEATHER.get(d['weather_code'][i], 'Unbekannt')}, "
               f"{fmt(d['temperature_2m_min'][i], 0)}–{fmt(d['temperature_2m_max'][i], 0)} °C, "
               f"{fmt(d['sunshine_duration'][i] / 3600)} h Sonne")

    if car_hours:
        title = f"{label}: {fmt(pv_total, 0)} kWh Sonne, Auto +{fmt(car_percent(car_total), 0)} %"
        lines = [f"Auto laden {car_hours[0]['hour']}–{car_hours[-1]['hour'] + 1} Uhr: "
                 f"{fmt(car_total, 0)} kWh ≈ +{fmt(car_percent(car_total), 0)} % Akku"]
        lines += [f"{x['hour']}–{x['hour'] + 1} Uhr: {fmt(x['car'])} kW (+{fmt(car_percent(x['car']), 0)} %)"
                  for x in car_hours]
    else:
        title = f"{label}: {fmt(pv_total, 0)} kWh Sonne, kein Laden fürs Auto"
        peak = max(hours, key=lambda x: x["pv"], default=None)
        lines = ["Heute reicht der Überschuss nicht für die Wallbox."]
        if peak and peak["pv"] > 0.05:
            lines.append(f"Meiste Sonne {peak['hour']}–{peak['hour'] + 1} Uhr mit {fmt(peak['pv'])} kW.")
    lines.append(f"Wetter: {weather}")

    return {"title": title, "message": "\n".join(lines)}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    # Ortszeit, auch auf einem Server in UTC
    now = datetime.now(ZoneInfo("Europe/Berlin")).replace(tzinfo=None)
    day = datetime.fromisoformat(args[0]) if args else now
    report = build_report(fetch_forecast(), now, day)
    print(report["title"])
    print(report["message"])


if __name__ == "__main__":
    main()
