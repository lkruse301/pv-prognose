// PV-Ertragsmodell, Speicher-/Wallbox-Simulation und Datenabruf (Open-Meteo).
// Klassisches Script (kein ES-Modul), damit index.html auch per Doppelklick läuft.

const PV = (() => {
  const DEFAULTS = {
    // Standort: Schling, Detmold-Heiligenkirchen
    lat: 51.9045,
    lon: 8.8563,
    tilt: 35,          // Grad; für diesen Standort ertragsoptimal (geprüft mit Open-Meteo-Archiv)
    azimuth: 0,        // Open-Meteo-Konvention: 0 = Süd, -90 = Ost, 90 = West
    kwp: 10.0,         // laut FusionSolar (String-Gesamtleistung)
    inverterKw: 11,    // AC-Begrenzung Wechselrichter (SUN2000-10KTL, gemessene Spitze 11 kW)
    losses: 14,        // % Systemverluste (Kabel, Wechselrichter, Verschmutzung, Mismatch)
    tempCoeff: -0.35,  // %/K Leistungs-Temperaturkoeffizient
    bifacial: true,
    bifaciality: 0.7,  // Rückseiten-Wirkungsgrad relativ zur Vorderseite
    albedo: 0.2,       // Bodenreflexion (Wiese ca. 0,2)
    batteryKwh: 10,    // LUNA2000
    batteryMaxKw: 5,   // max. Lade-/Entladeleistung
    batteryMinSoc: 5,  // % Reserve, die nicht entladen wird
    batterySoc: 50,    // % angenommener Stand des Hausspeichers zu Beginn der Prognose
    baseLoadKwhDay: 7, // Hausverbrauch pro Tag ohne Auto (sparsamste 10 % der Tage laut FusionSolar)
    wallboxMinKw: 4.1, // 6 A dreiphasig
    wallboxMaxKw: 11,
    phaseSwitching: true, // Wallbox schaltet bei wenig Überschuss auf einphasig um
    wallboxMin1pKw: 1.4,  // 6 A einphasig
    wallboxMax1pKw: 3.7,  // 16 A einphasig
    carFirst: false,   // true = Überschuss zuerst ins Auto, dann in den Akku
    carBatteryKwh: 60, // Akku des E-Autos
    chargeLossPct: 10, // % Ladeverluste zwischen Wallbox und Auto-Akku
    horizon: [],       // Horizonthöhe in Grad für −180° … 180° (PVGIS), in config.json gesetzt
    calibration: [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1], // Korrekturfaktor je Monat aus echten Erträgen
    weatherCorrection: null, // { a, b }: Faktor a + b·Klarheit des Tages (kalibrieren.py)
    band: null,        // { limits, ranges }: 10–90-%-Bereich je Wetterlage (kalibrieren.py)
  };

  // Rückseiten-Abschattung durch Gestell und Module selbst (empirischer Faktor)
  const REAR_SHADING = 0.6;
  // Faiman-Modell für die Zelltemperatur (pvlib-Standardwerte)
  const FAIMAN_U0 = 25;
  const FAIMAN_U1 = 6.84;

  const HISTORY_FIRST_YEAR = 2015;
  const HISTORY_LAST_YEAR = 2024;

  // AC-Leistung in kW (= kWh in dieser Stunde, da Open-Meteo Stundenmittel liefert)
  function acPowerKw(s, gti, ghi, dhi, tempC, windKmh) {
    if (!gti || gti <= 0) return 0;
    const beta = (s.tilt * Math.PI) / 180;
    let rear = 0;
    if (s.bifacial) {
      rear = (s.albedo * ghi * (1 + Math.cos(beta)) / 2 + dhi * (1 - Math.cos(beta)) / 2) * REAR_SHADING;
    }
    const geff = gti + s.bifaciality * Math.max(rear, 0);
    const wind = (windKmh ?? 5.4) / 3.6;
    const tCell = tempC + geff / (FAIMAN_U0 + FAIMAN_U1 * wind);
    const pDc = (s.kwp * geff) / 1000 * (1 + (s.tempCoeff / 100) * (tCell - 25));
    return Math.max(0, Math.min(pDc * (1 - s.losses / 100), s.inverterKw));
  }

  // Simuliert eine Stunde: Hauslast, Akku, Überschuss fürs Auto.
  // state.soc in kWh wird fortgeschrieben.
  function simulateHour(s, state, pvKwh) {
    const load = s.baseLoadKwhDay / 24;
    const cap = s.batteryKwh;
    const minSoc = (cap * s.batteryMinSoc) / 100;
    let net = pvKwh - load;
    let car = 0;

    const carFrom = (surplus) => {
      if (surplus >= s.wallboxMinKw) return Math.min(surplus, s.wallboxMaxKw);
      if (s.phaseSwitching && surplus >= s.wallboxMin1pKw) return Math.min(surplus, s.wallboxMax1pKw);
      return 0;
    };

    if (net > 0 && s.carFirst) {
      car = carFrom(net);
      net -= car;
    }
    if (net > 0) {
      const charge = Math.min(net, s.batteryMaxKw, Math.max(cap - state.soc, 0));
      state.soc += charge;
      net -= charge;
      if (!s.carFirst) {
        car = carFrom(net);
        net -= car;
      }
    } else if (net < 0) {
      const discharge = Math.min(-net, s.batteryMaxKw, Math.max(state.soc - minSoc, 0));
      state.soc -= discharge;
      net += discharge;
    }
    return { car, gridExport: Math.max(net, 0), gridImport: Math.max(-net, 0) };
  }

  // Open-Meteo-Zeitstempel "2026-10-30T12:00" = Mittelwert der Stunde 11–12 Uhr (Ortszeit).
  function intervalStart(timeStr) {
    const [d, t] = timeStr.split('T');
    const [y, m, day] = d.split('-').map(Number);
    return new Date(y, m - 1, day, Number(t.slice(0, 2)) - 1);
  }

  function dayKey(date) {
    const p = (n) => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())}`;
  }

  // ---- Horizont-Abschattung ----
  // Sonnenstand nach NOAA/Spencer: cos(Zenit) und Azimut (0 = Süd, positiv nach Westen) in Grad
  function sunPosition(s, date) {
    const startOfYear = Date.UTC(date.getUTCFullYear(), 0, 1);
    const doy = Math.floor((date.getTime() - startOfYear) / 86400e3) + 1;
    const hour = date.getUTCHours() + date.getUTCMinutes() / 60;
    const g = (2 * Math.PI / 365) * (doy - 1 + (hour - 12) / 24);
    const eot = 229.18 * (0.000075 + 0.001868 * Math.cos(g) - 0.032077 * Math.sin(g)
      - 0.014615 * Math.cos(2 * g) - 0.040849 * Math.sin(2 * g));
    const decl = 0.006918 - 0.399912 * Math.cos(g) + 0.070257 * Math.sin(g) - 0.006758 * Math.cos(2 * g)
      + 0.000907 * Math.sin(2 * g) - 0.002697 * Math.cos(3 * g) + 0.00148 * Math.sin(3 * g);
    const ha = ((hour * 60 + eot + 4 * s.lon) / 4 - 180) * Math.PI / 180;
    const lat = s.lat * Math.PI / 180;
    const cosZ = Math.sin(lat) * Math.sin(decl) + Math.cos(lat) * Math.cos(decl) * Math.cos(ha);
    const az = Math.atan2(Math.sin(ha), Math.cos(ha) * Math.sin(lat) - Math.tan(decl) * Math.cos(lat)) * 180 / Math.PI;
    return { cosZ, az };
  }

  // Horizonthöhe in Grad; s.horizon enthält Werte für −180° … 180° in gleichen Schritten (PVGIS)
  function horizonHeight(s, az) {
    const hz = s.horizon;
    if (!hz?.length) return 0;
    const step = 360 / (hz.length - 1);
    const x = (az + 180) / step;
    const i = Math.min(Math.floor(x), hz.length - 2);
    return hz[i] + (hz[i + 1] - hz[i]) * (x - i);
  }

  // Direktstrahlung auf der Modulfläche, die in dieser Stunde hinter dem Horizont verschwindet (W/m²).
  // Vier Stichproben pro Stunde, damit Sonnenauf- und -untergang hinter Hügeln anteilig zählen.
  function hiddenBeam(s, start, dni) {
    if (!dni || dni <= 0 || !s.horizon?.length) return 0;
    const beta = s.tilt * Math.PI / 180;
    let lost = 0;
    for (const min of [7.5, 22.5, 37.5, 52.5]) {
      const { cosZ, az } = sunPosition(s, new Date(start.getTime() + min * 60e3));
      if (cosZ <= 0) continue;
      const elevation = 90 - Math.acos(cosZ) * 180 / Math.PI;
      if (elevation >= horizonHeight(s, az)) continue;
      const sinZ = Math.sqrt(1 - cosZ * cosZ);
      const cosTheta = cosZ * Math.cos(beta) + sinZ * Math.sin(beta) * Math.cos((az - s.azimuth) * Math.PI / 180);
      lost += dni * Math.max(cosTheta, 0) / 4;
    }
    return lost;
  }

  // AC-Ertrag einer Stunde: Horizont-Abschattung, Anlagenmodell, Kalibrierfaktor des Monats
  function hourlyPv(s, h, i, start) {
    const gti = Math.max((h.global_tilted_irradiance[i] ?? 0) - hiddenBeam(s, start, h.direct_normal_irradiance?.[i]), 0);
    const pv = acPowerKw(s, gti, h.shortwave_radiation[i], h.diffuse_radiation[i], h.temperature_2m[i], h.wind_speed_10m[i]);
    return pv * (s.calibration?.[start.getMonth()] ?? 1);
  }

  // ---- Wetterabhängige Korrektur und Bandbreite (Werte aus kalibrieren.py) ----
  // Globalstrahlung bei wolkenlosem Himmel (Haurwitz), Mitte der Stunde
  function clearSkyGhi(s, start) {
    const { cosZ } = sunPosition(s, new Date(start.getTime() + 30 * 60e3));
    return cosZ > 0.01 ? 1098 * cosZ * Math.exp(-0.057 / cosZ) : 0;
  }

  // Klarheit je Tag: vorhergesagte Globalstrahlung im Verhältnis zum wolkenlosen Himmel (0 = trüb, 1 = klar)
  function clarityByDay(s, h) {
    const sums = new Map();
    for (let i = 0; i < h.time.length; i++) {
      const start = intervalStart(h.time[i]);
      const key = dayKey(start);
      const e = sums.get(key) ?? [0, 0];
      e[0] += h.shortwave_radiation[i] ?? 0;
      e[1] += clearSkyGhi(s, start);
      sums.set(key, e);
    }
    return new Map([...sums].filter(([, [, c]]) => c > 0).map(([k, [g, c]]) => [k, Math.min(g / c, 1.2)]));
  }

  function weatherFactor(s, clarity) {
    const w = s.weatherCorrection;
    if (!w || clarity == null) return 1;
    return Math.min(Math.max(w.a + w.b * clarity, 0.4), 1.3);
  }

  // Wahrscheinlicher Bereich (10–90 %) eines Tagesertrags, abhängig von der Wetterlage
  function band(s, pv, clarity) {
    const b = s.band;
    if (!b || clarity == null) return null;
    const i = b.limits.filter((limit) => clarity >= limit).length;
    const [lo, hi] = b.ranges[i];
    return [pv * lo, pv * hi];
  }

  async function fetchJson(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`Wetterdienst antwortet mit Fehler ${res.status}`);
    return res.json();
  }

  function baseParams(s) {
    return `latitude=${s.lat}&longitude=${s.lon}&tilt=${s.tilt}&azimuth=${s.azimuth}&timezone=Europe%2FBerlin`;
  }

  // ---- Kurzfrist: Wetterprognose für heute + 7 Tage ----
  // Weiter voraus ist die Einstrahlungsprognose zu unsicher für die Ladeplanung.
  async function shortTerm(s) {
    const url = `https://api.open-meteo.com/v1/forecast?${baseParams(s)}&forecast_days=8` +
      '&hourly=global_tilted_irradiance,shortwave_radiation,diffuse_radiation,direct_normal_irradiance,temperature_2m,wind_speed_10m,cloud_cover,snow_depth' +
      '&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,sunshine_duration,wind_speed_10m_max';
    const data = await fetchJson(url);
    const h = data.hourly;
    const dl = data.daily;
    const weather = new Map(dl.time.map((t, i) => [t, {
      code: dl.weather_code[i],
      tMax: dl.temperature_2m_max[i],
      tMin: dl.temperature_2m_min[i],
      precip: dl.precipitation_sum[i],
      precipProb: dl.precipitation_probability_max[i],
      sunHours: dl.sunshine_duration[i] / 3600,
      windMax: dl.wind_speed_10m_max[i],
    }]));
    // Ab 0 Uhr heute rechnen, damit der heutige Tag vollständig angezeigt wird.
    // Der Hausspeicher-Stand um Mitternacht ist eine Annahme (batterySoc).
    const now = new Date();
    const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const state = { soc: (s.batteryKwh * s.batterySoc) / 100 };
    const days = new Map();
    const clarity = clarityByDay(s, h);

    for (let i = 0; i < h.time.length; i++) {
      const start = intervalStart(h.time[i]);
      const end = new Date(start.getTime() + 3600e3);
      if (start < midnight) continue;
      const key = dayKey(start);
      const pv = hourlyPv(s, h, i, start) * weatherFactor(s, clarity.get(key));
      const sim = simulateHour(s, state, pv);
      if (!weather.has(key)) continue;
      if (!days.has(key)) {
        days.set(key, { key, date: new Date(start.getFullYear(), start.getMonth(), start.getDate()),
          weather: weather.get(key), clarity: clarity.get(key), snowDepth: 0, hours: [] });
      }
      const day = days.get(key);
      day.snowDepth = Math.max(day.snowDepth, h.snow_depth?.[i] ?? 0);
      days.get(key).hours.push({
        hour: start.getHours(),
        pv,
        car: sim.car,
        soc: (state.soc / s.batteryKwh) * 100,
        cloud: h.cloud_cover[i],
        past: end <= now,
      });
    }
    return [...days.values()].map((d) => summarizeDay(s, d));
  }

  function summarizeDay(s, d) {
    const pv = d.hours.reduce((a, x) => a + x.pv, 0);
    const car = d.hours.reduce((a, x) => a + x.car, 0);
    const peak = Math.max(...d.hours.map((x) => x.pv));
    // längstes zusammenhängendes Fenster mit Auto-Überschuss
    let best = null, cur = null;
    for (const x of d.hours) {
      if (x.car > 0) {
        if (!cur) cur = { from: x.hour, to: x.hour + 1, kwh: 0 };
        cur.to = x.hour + 1;
        cur.kwh += x.car;
        if (!best || cur.kwh > best.kwh) best = { ...cur };
      } else {
        cur = null;
      }
    }
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const lead = Math.round((d.date - today) / 86400e3);
    const reliability = lead <= 2 ? 'hoch' : lead <= 6 ? 'mittel' : 'gering';
    let rating = 'schlecht';
    if (car >= 15) rating = 'sehr gut';
    else if (car >= 7) rating = 'gut';
    else if (car > 0) rating = 'mäßig';
    // Schneewarnung: mehr als 1 cm Schneedecke gemeldet und tagsüber kaum Tauwetter
    const snow = d.snowDepth > 0.01 && d.weather.tMax < 3;
    return { ...d, pv, car, peak, best, lead, reliability, rating, range: band(s, pv, d.clarity), snow };
  }

  // Morgens gespeicherte Prognose (prognosen/JJJJ-MM-TT.json) wie einen Tag der Vorhersage aufbereiten
  function savedDay(s, saved) {
    const [y, m, day] = saved.date.split('-').map(Number);
    const d = summarizeDay(s, { key: saved.date, date: new Date(y, m - 1, day), weather: saved.weather, snowDepth: 0, hours: saved.hours });
    return { ...d, range: saved.range, snow: saved.snow };
  }

  // ---- Langfrist: Statistik aus den Jahren 2015–2024 (Open-Meteo-Archiv) ----
  function cacheKey(s) {
    const keys = ['lat', 'lon', 'tilt', 'azimuth', 'kwp', 'inverterKw', 'losses', 'tempCoeff', 'bifacial',
      'bifaciality', 'albedo', 'batteryKwh', 'batteryMaxKw', 'batteryMinSoc', 'baseLoadKwhDay',
      'wallboxMinKw', 'wallboxMaxKw', 'phaseSwitching', 'wallboxMin1pKw', 'wallboxMax1pKw', 'carFirst', 'horizon', 'calibration'];
    return 'pv-history-v3:' + JSON.stringify([s.weatherCorrection]) + ':' + keys.map((k) => s[k]).join('|');
  }

  // Liefert { "2019": { "07-14": [pvKwh, carKwh], ... }, ... }
  async function history(s, onProgress) {
    const key = cacheKey(s);
    try {
      const cached = localStorage.getItem(key);
      if (cached) return JSON.parse(cached);
    } catch (_) { /* kein Cache verfügbar */ }

    onProgress?.('Lade Wetterdaten 2015–2024 (ca. 5 MB) …');
    const url = `https://archive-api.open-meteo.com/v1/archive?${baseParams(s)}` +
      `&start_date=${HISTORY_FIRST_YEAR}-01-01&end_date=${HISTORY_LAST_YEAR}-12-31` +
      '&hourly=global_tilted_irradiance,shortwave_radiation,diffuse_radiation,direct_normal_irradiance,temperature_2m,wind_speed_10m';
    const data = await fetchJson(url);
    onProgress?.('Berechne Erträge …');
    const h = data.hourly;
    const result = {};
    const state = { soc: (s.batteryKwh * s.batterySoc) / 100 };
    const clarity = clarityByDay(s, h);
    for (let i = 0; i < h.time.length; i++) {
      const start = intervalStart(h.time[i]);
      const pv = hourlyPv(s, h, i, start) * weatherFactor(s, clarity.get(dayKey(start)));
      const sim = simulateHour(s, state, pv);
      const year = start.getFullYear();
      if (year < HISTORY_FIRST_YEAR) continue;
      const md = dayKey(start).slice(5);
      const y = (result[year] ??= {});
      const cell = (y[md] ??= [0, 0]);
      cell[0] += pv;
      cell[1] += sim.car;
    }
    for (const y of Object.values(result)) {
      for (const md of Object.keys(y)) y[md] = [Math.round(y[md][0] * 100) / 100, Math.round(y[md][1] * 100) / 100];
    }
    try {
      for (let i = localStorage.length - 1; i >= 0; i--) {
        const k = localStorage.key(i);
        if (k.startsWith('pv-history-')) localStorage.removeItem(k);
      }
      localStorage.setItem(key, JSON.stringify(result));
    } catch (_) { /* Cache voll oder gesperrt – dann eben beim nächsten Mal neu laden */ }
    return result;
  }

  // Summe pro historischem Jahr für den Kalenderzeitraum [from, to] (Date-Objekte, inkl.)
  function rangeStats(hist, from, to) {
    const days = [];
    for (let d = new Date(from); d <= to; d.setDate(d.getDate() + 1)) days.push(dayKey(d).slice(5));
    const perYear = Object.keys(hist).map((year) => {
      let pv = 0, car = 0;
      for (const md of days) {
        const v = hist[year][md] ?? (md === '02-29' ? null : [0, 0]);
        if (v) { pv += v[0]; car += v[1]; }
      }
      return { year: Number(year), pv, car };
    });
    return { days: days.length, perYear, pv: quantiles(perYear.map((x) => x.pv)), car: quantiles(perYear.map((x) => x.car)) };
  }

  function monthStats(hist) {
    const months = [];
    for (let m = 1; m <= 12; m++) {
      const mm = String(m).padStart(2, '0');
      const sums = Object.values(hist).map((y) =>
        Object.entries(y).filter(([md]) => md.startsWith(mm)).reduce((a, [, v]) => a + v[0], 0));
      months.push(quantiles(sums));
    }
    return months;
  }

  function quantiles(values) {
    const v = [...values].sort((a, b) => a - b);
    const q = (p) => {
      const idx = (v.length - 1) * p;
      const lo = Math.floor(idx), hi = Math.ceil(idx);
      return v[lo] + (v[hi] - v[lo]) * (idx - lo);
    };
    return { min: v[0], p10: q(0.1), median: q(0.5), p90: q(0.9), max: v[v.length - 1] };
  }

  return { DEFAULTS, savedDay, shortTerm, history, rangeStats, monthStats, acPowerKw, simulateHour };
})();
