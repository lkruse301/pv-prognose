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


class FetchError(Exception):
    pass


def curl(args):
    # curl statt urllib: die python.org-Installation auf macOS hat oft keine Zertifikate eingerichtet
    out = subprocess.run(["curl", "-sS", "--max-time", "30", "-w", "\n%{http_code}", *args],
                         capture_output=True, text=True)
    body, _, status = out.stdout.rpartition("\n")
    if out.returncode != 0 or status != "200":
        # Statuscode und Anfang der Antwort zeigen, ob der Wetterdienst oder ein Proxy ablehnt
        raise FetchError(f"curl-Code {out.returncode}, HTTP {status or '-'} {out.stderr.strip()} {body[:200]}")
    return body


def fetch_open_meteo():
    url = (
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={S['lat']}&longitude={S['lon']}&tilt={S['tilt']}&azimuth={S['azimuth']}"
        "&timezone=Europe%2FBerlin&forecast_days=2"
        "&hourly=global_tilted_irradiance,shortwave_radiation,diffuse_radiation,direct_normal_irradiance,"
        "temperature_2m,wind_speed_10m,snow_depth"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,sunshine_duration"
    )
    data = json.loads(curl([url]))
    data["source"] = "Open-Meteo"
    # Schnee: irgendwann am Tag mehr als 1 cm Schneedecke gemeldet
    depth = {}
    for t, v in zip(data["hourly"]["time"], data["hourly"]["snow_depth"]):
        k = (datetime.fromisoformat(t) - timedelta(hours=1)).strftime("%Y-%m-%d")
        depth[k] = max(depth.get(k, 0), v or 0)
    data["daily"]["snow"] = [depth.get(k, 0) > 0.01 for k in data["daily"]["time"]]
    return data


# ---- Ausweichquelle: DWD-Vorhersage (MOSMIX) über Bright Sky ----
# Open-Meteo begrenzt kostenlose Abrufe pro IP-Adresse; Cloud-Server teilen sich Adressen
# und laufen deshalb manchmal ins Tageslimit. Bright Sky liefert nur die Globalstrahlung,
# die Einstrahlung auf die geneigte Fläche wird hier selbst berechnet.

ICON_TEXT = {
    "clear-day": "Klar", "clear-night": "Klar", "partly-cloudy-day": "Teilweise bewölkt",
    "partly-cloudy-night": "Teilweise bewölkt", "cloudy": "Bedeckt", "fog": "Nebel", "wind": "Windig",
    "rain": "Regen", "sleet": "Schneeregen", "snow": "Schnee", "hail": "Hagel", "thunderstorm": "Gewitter",
}


def sun_position(t_utc):
    """Kosinus des Zenitwinkels und Azimut (0 = Süd, positiv nach Westen) in Grad."""
    doy = t_utc.timetuple().tm_yday
    hour = t_utc.hour + t_utc.minute / 60
    g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
    eot = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    solar_minutes = hour * 60 + eot + 4 * S["lon"]
    ha = math.radians(solar_minutes / 4 - 180)
    lat = math.radians(S["lat"])
    cos_z = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(ha)
    az = math.degrees(math.atan2(math.sin(ha), math.cos(ha) * math.sin(lat) - math.tan(decl) * math.cos(lat)))
    return cos_z, az, doy


def tilted_irradiance(ghi, t_mid_utc):
    """GHI → (GTI, DHI, DNI) mit Erbs-Aufteilung und isotropem Himmelsmodell."""
    if ghi <= 0:
        return 0.0, 0.0, 0.0
    cos_z, az, doy = sun_position(t_mid_utc)
    beta = math.radians(S["tilt"])
    if cos_z < 0.05:
        return ghi * (1 + math.cos(beta)) / 2, ghi, 0.0
    g0 = 1367 * (1 + 0.033 * math.cos(2 * math.pi * doy / 365)) * cos_z
    kt = min(ghi / g0, 1.0)
    if kt <= 0.22:
        kd = 1 - 0.09 * kt
    elif kt <= 0.8:
        kd = 0.9511 - 0.1604 * kt + 4.388 * kt ** 2 - 16.638 * kt ** 3 + 12.336 * kt ** 4
    else:
        kd = 0.165
    dhi = kd * ghi
    dni = (ghi - dhi) / cos_z
    sin_z = math.sqrt(max(0.0, 1 - cos_z ** 2))
    cos_theta = cos_z * math.cos(beta) + sin_z * math.sin(beta) * math.cos(math.radians(az - S["azimuth"]))
    gti = dni * max(cos_theta, 0) + dhi * (1 + math.cos(beta)) / 2 + ghi * S["albedo"] * (1 - math.cos(beta)) / 2
    return gti, dhi, dni


def fetch_bright_sky(now):
    start = now.strftime("%Y-%m-%d")
    end = (now + timedelta(days=3)).strftime("%Y-%m-%d")
    url = (f"https://api.brightsky.dev/weather?lat={S['lat']}&lon={S['lon']}"
           f"&date={start}T00:00%2B01:00&last_date={end}T00:00%2B01:00&tz=Europe/Berlin")
    rows = json.loads(curl([url]))["weather"]
    berlin = ZoneInfo("Europe/Berlin")
    hourly = {k: [] for k in ("time", "global_tilted_irradiance", "shortwave_radiation",
                              "diffuse_radiation", "direct_normal_irradiance", "temperature_2m", "wind_speed_10m")}
    days = {}
    for r in rows:
        t = datetime.fromisoformat(r["timestamp"])          # Ende der Stunde
        ghi = (r.get("solar") or 0) * 1000                  # kWh/m² in der Stunde → mittlere W/m²
        gti, dhi, dni = tilted_irradiance(ghi, (t - timedelta(minutes=30)).astimezone(ZoneInfo("UTC")))
        local = t.astimezone(berlin).replace(tzinfo=None)
        hourly["time"].append(local.strftime("%Y-%m-%dT%H:%M"))
        hourly["global_tilted_irradiance"].append(gti)
        hourly["shortwave_radiation"].append(ghi)
        hourly["diffuse_radiation"].append(dhi)
        hourly["direct_normal_irradiance"].append(dni)
        hourly["temperature_2m"].append(r.get("temperature") or 0)
        hourly["wind_speed_10m"].append(r.get("wind_speed"))
        start_local = local - timedelta(hours=1)
        d = days.setdefault(start_local.strftime("%Y-%m-%d"), {"temps": [], "rain": 0.0, "sun": 0.0, "icons": []})
        if r.get("temperature") is not None:
            d["temps"].append(r["temperature"])
        d["rain"] += r.get("precipitation") or 0
        d["sun"] += (r.get("sunshine") or 0) * 60
        if 8 <= start_local.hour < 18 and r.get("icon"):
            d["icons"].append(r["icon"])
        d["snow"] = d.get("snow", False) or r.get("icon") in ("snow", "sleet")
    keys = [k for k, v in days.items() if v["temps"]]
    daily = {
        "time": keys,
        "text": [ICON_TEXT.get(max(set(days[k]["icons"]), key=days[k]["icons"].count), "Unbekannt")
                 if days[k]["icons"] else "Unbekannt" for k in keys],
        "temperature_2m_min": [min(days[k]["temps"]) for k in keys],
        "temperature_2m_max": [max(days[k]["temps"]) for k in keys],
        "precipitation_sum": [days[k]["rain"] for k in keys],
        "sunshine_duration": [days[k]["sun"] for k in keys],
        "snow": [days[k].get("snow", False) for k in keys],
    }
    return {"hourly": hourly, "daily": daily, "source": "DWD über Bright Sky"}


# ---- Horizont-Abschattung (wie hiddenBeam in js/pv.js) ----
def horizon_height(az):
    hz = S.get("horizon") or []
    if not hz:
        return 0.0
    step = 360 / (len(hz) - 1)
    x = (az + 180) / step
    i = min(int(x), len(hz) - 2)
    return hz[i] + (hz[i + 1] - hz[i]) * (x - i)


def hidden_beam(start_local, dni):
    """Direktstrahlung auf der Modulfläche, die in dieser Stunde hinter dem Horizont liegt (W/m²)."""
    if not dni or dni <= 0 or not S.get("horizon"):
        return 0.0
    beta = math.radians(S["tilt"])
    start_utc = start_local.replace(tzinfo=ZoneInfo("Europe/Berlin")).astimezone(ZoneInfo("UTC"))
    lost = 0.0
    for minutes in (7.5, 22.5, 37.5, 52.5):
        cos_z, az, _ = sun_position(start_utc + timedelta(minutes=minutes))
        if cos_z <= 0:
            continue
        elevation = 90 - math.degrees(math.acos(min(cos_z, 1.0)))
        if elevation >= horizon_height(az):
            continue
        sin_z = math.sqrt(max(0.0, 1 - cos_z ** 2))
        cos_theta = cos_z * math.cos(beta) + sin_z * math.sin(beta) * math.cos(math.radians(az - S["azimuth"]))
        lost += dni * max(cos_theta, 0) / 4
    return lost


def hourly_pv(h, i, start):
    """AC-Ertrag einer Stunde: Horizont, Anlagenmodell, Kalibrierfaktor des Monats (wie hourlyPv in js/pv.js)."""
    dni = (h.get("direct_normal_irradiance") or [None] * len(h["time"]))[i]
    gti = max((h["global_tilted_irradiance"][i] or 0) - hidden_beam(start, dni), 0)
    pv = ac_power_kw(gti, h["shortwave_radiation"][i], h["diffuse_radiation"][i],
                     h["temperature_2m"][i], h["wind_speed_10m"][i])
    return pv * (S.get("calibration") or [1] * 12)[start.month - 1]


# ---- Wetterabhängige Korrektur, Bandbreite, Schnee (Werte aus kalibrieren.py in config.json) ----
def clear_sky_ghi(start_local):
    """Globalstrahlung bei wolkenlosem Himmel (Haurwitz), Mitte der Stunde, W/m²."""
    t = start_local.replace(tzinfo=ZoneInfo("Europe/Berlin")).astimezone(ZoneInfo("UTC")) + timedelta(minutes=30)
    cos_z, _, _ = sun_position(t)
    return 1098 * cos_z * math.exp(-0.057 / cos_z) if cos_z > 0.01 else 0.0


def clarity_by_day(h):
    """Klarheit je Tag: vorhergesagte Globalstrahlung im Verhältnis zum wolkenlosen Himmel (0 = trüb, 1 = klar)."""
    sums = {}
    for i, t in enumerate(h["time"]):
        start = datetime.fromisoformat(t) - timedelta(hours=1)
        g = sums.setdefault(start.date(), [0.0, 0.0])
        g[0] += h["shortwave_radiation"][i] or 0
        g[1] += clear_sky_ghi(start)
    return {d: min(g / c, 1.2) for d, (g, c) in sums.items() if c > 0}


def weather_factor(clarity):
    w = S.get("weatherCorrection")
    if not w or clarity is None:
        return 1.0
    return min(max(w["a"] + w["b"] * clarity, 0.4), 1.3)


def band(pv, clarity):
    """Wahrscheinlicher Bereich (10–90 %) für einen Tagesertrag."""
    b = S.get("band")
    if not b or clarity is None:
        return None
    i = sum(1 for limit in b["limits"] if clarity >= limit)
    lo, hi = b["ranges"][i]
    return pv * lo, pv * hi


def fetch_forecast(now):
    try:
        return fetch_open_meteo()
    except FetchError as err:
        print(f"Hinweis: Open-Meteo nicht verfügbar ({err}), nutze DWD-Vorhersage.", file=sys.stderr)
    try:
        return fetch_bright_sky(now)
    except FetchError as err:
        sys.exit(f"Abruf fehlgeschlagen: Open-Meteo und Bright Sky nicht erreichbar ({err})")


def build_report(data, now, day):
    h = data["hourly"]
    key = day.strftime("%Y-%m-%d")
    clarity = clarity_by_day(h)
    state = {"soc": S["batteryKwh"] * S["batterySoc"] / 100}
    hours = []
    for i, t in enumerate(h["time"]):
        # Zeitstempel = Mittel der Stunde davor (11:00 → 10–11 Uhr)
        start = datetime.fromisoformat(t) - timedelta(hours=1)
        if start + timedelta(hours=1) <= now.replace(minute=0, second=0, microsecond=0):
            continue
        pv = hourly_pv(h, i, start) * weather_factor(clarity.get(start.date()))
        car = simulate_hour(state, pv)
        if start.strftime("%Y-%m-%d") == key:
            hours.append({"hour": start.hour, "pv": pv, "car": car})

    d = data["daily"]
    i = d["time"].index(key)
    pv_total = sum(x["pv"] for x in hours)
    car_total = sum(x["car"] for x in hours)
    car_hours = [x for x in hours if x["car"] > 0]
    label = f"{WEEKDAYS[day.weekday()]} {day.day}.{day.month}."
    text = d["text"][i] if "text" in d else WEATHER.get(d["weather_code"][i], "Unbekannt")
    weather = (f"{text}, "
               f"{fmt(d['temperature_2m_min'][i], 0)}–{fmt(d['temperature_2m_max'][i], 0)} °C, "
               f"{fmt(d['sunshine_duration'][i] / 3600)} h Sonne")

    rng = band(pv_total, clarity.get(day.date()))
    span = f" ({fmt(rng[0], 0)}–{fmt(rng[1], 0)})" if rng and pv_total >= 1 else ""
    snow = d.get("snow", [False] * len(d["time"]))[i] and d["temperature_2m_max"][i] < 3

    if car_hours:
        title = f"{label}: {fmt(pv_total, 0)}{span} kWh Sonne, Auto +{fmt(car_percent(car_total), 0)} %"
        lines = [f"Auto laden {car_hours[0]['hour']}–{car_hours[-1]['hour'] + 1} Uhr: "
                 f"{fmt(car_total, 0)} kWh ≈ +{fmt(car_percent(car_total), 0)} % Akku"]
        lines += [f"{x['hour']}–{x['hour'] + 1} Uhr: {fmt(x['car'])} kW (+{fmt(car_percent(x['car']), 0)} %)"
                  for x in car_hours]
    else:
        title = f"{label}: {fmt(pv_total, 0)}{span} kWh Sonne, kein Laden fürs Auto"
        peak = max(hours, key=lambda x: x["pv"], default=None)
        lines = ["Heute reicht der Überschuss nicht für die Wallbox."]
        if peak and peak["pv"] > 0.05:
            lines.append(f"Meiste Sonne {peak['hour']}–{peak['hour'] + 1} Uhr mit {fmt(peak['pv'])} kW.")
    lines.append(f"Wetter: {weather}")
    if snow:
        lines.append("Achtung: Schnee auf den Modulen möglich – der Ertrag kann deutlich geringer ausfallen.")
    if data.get("source") != "Open-Meteo":
        lines.append(f"Quelle: {data['source']}")

    return {"title": title, "message": "\n".join(lines)}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    # Ortszeit, auch auf einem Server in UTC
    now = datetime.now(ZoneInfo("Europe/Berlin")).replace(tzinfo=None)
    day = datetime.fromisoformat(args[0]) if args else now
    report = build_report(fetch_forecast(now), now, day)
    print(report["title"])
    print(report["message"])


if __name__ == "__main__":
    main()
