// Oberfläche: Ansichten, Einstellungen, Ladeplan, Diagramme.

const $ = (sel) => document.querySelector(sel);
const fmt = (n, d = 1) => n.toLocaleString('de-DE', { minimumFractionDigits: d, maximumFractionDigits: d });
const WEEKDAYS = ['So', 'Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa'];
const BAND_FIRST_HOUR = 5;
const BAND_LAST_HOUR = 20; // letzte Stunde 20–21 Uhr
const charts = {};

// ---------- Einstellungen ----------
// Die Anlagenwerte stehen in config.json, damit alle Geräte und der Morgenbericht dieselben nutzen.
// Fehlt die Datei (z. B. beim Öffnen per Doppelklick), gelten die Standardwerte aus pv.js.
let settings = { ...PV.DEFAULTS };

async function loadConfig() {
  try {
    const res = await fetch('config.json', { cache: 'no-cache' });
    if (res.ok) settings = { ...PV.DEFAULTS, ...(await res.json()) };
  } catch (_) { /* Standardwerte behalten */ }
}

function fillForm() {
  const form = $('#settingsForm');
  for (const [k, v] of Object.entries(settings)) {
    const el = form.elements[k];
    if (!el) continue;
    if (el.type === 'checkbox') el.checked = v; else el.value = v;
  }
  const hz = settings.horizon ?? [];
  if (hz.length) {
    const step = 360 / (hz.length - 1);
    // höchster Punkt im Süden (−90° … 90°), weil nur dort die Sonne steht, wenn sie Ertrag bringt
    const south = hz.map((h, i) => [h, i * step - 180]).filter(([, a]) => a >= -90 && a <= 90);
    const [maxH, maxA] = south.reduce((m, x) => (x[0] > m[0] ? x : m));
    const dir = maxA < -22.5 ? 'Südost' : maxA > 22.5 ? 'Südwest' : 'Süd';
    $('#horizonInfo').textContent = `bis ${fmt(maxH, 0)}° (${dir})`;
  } else {
    $('#horizonInfo').textContent = 'nicht berücksichtigt';
  }
  const cal = settings.calibration ?? [];
  $('#calibInfo').textContent = cal.every((f) => f === 1)
    ? 'noch nicht kalibriert'
    : `Faktor ${fmt(Math.min(...cal), 2)}–${fmt(Math.max(...cal), 2)} je Monat${settings.weatherCorrection ? ' + Wetterlage' : ''}`;
}

// ---------- Ansichten ----------
let activeTab = 'short';
document.querySelectorAll('.seg').forEach((btn) =>
  btn.addEventListener('click', () => {
    activeTab = btn.dataset.tab;
    document.querySelectorAll('.seg').forEach((b) => {
      b.classList.toggle('active', b === btn);
      b.setAttribute('aria-selected', b === btn);
    });
    document.querySelectorAll('.panel').forEach((p) => p.classList.toggle('active', p.id === 'tab-' + activeTab));
    window.scrollTo({ top: 0 });
    if (activeTab === 'long') loadMonths();
  }));

// ---------- Diagramme ----------
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function drawChart(id, config, ariaLabel) {
  charts[id]?.destroy();
  Chart.defaults.font.family = cssVar('--font');
  Chart.defaults.font.size = 12;
  Chart.defaults.color = cssVar('--secondary');
  Chart.defaults.borderColor = cssVar('--separator');
  const canvas = document.getElementById(id);
  canvas.setAttribute('aria-label', ariaLabel);
  charts[id] = new Chart(canvas, {
    ...config,
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: matchMedia('(prefers-reduced-motion: reduce)').matches ? false : { duration: 300 },
      interaction: { mode: 'index', intersect: false },
      ...config.options,
      plugins: {
        legend: { labels: { usePointStyle: true, pointStyle: 'rectRounded', boxWidth: 8, boxHeight: 8 } },
        ...config.options?.plugins,
      },
    },
  });
}
const kwhTooltip = { callbacks: { label: (c) => `${c.dataset.label}: ${fmt(c.parsed.y)} kWh` } };
const barStyle = { borderRadius: 4, borderSkipped: false };

// ---------- Prognose ----------
let shortDays = [];
let openKey = null;

function dayLabel(d) {
  if (d.lead === 0) return 'Heute';
  if (d.lead === 1) return 'Morgen';
  return `${WEEKDAYS[d.date.getDay()]} ${d.date.getDate()}.${d.date.getMonth() + 1}.`;
}

async function runShort() {
  const status = $('#shortStatus');
  status.className = 'status';
  status.textContent = 'Wetterprognose wird geladen …';
  try {
    shortDays = await PV.shortTerm(settings);
    const time = new Date().toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
    status.textContent = '';
    $('#subtitle').textContent = `Schling · ${fmt(settings.kwp)} kWp · Stand ${time} Uhr`;
    renderToday();
    renderDayList();
  } catch (err) {
    status.className = 'status error';
    status.textContent = `Die Wetterprognose konnte nicht geladen werden (${err.message}). Prüfe die Internetverbindung und tippe erneut auf „Prognose aktualisieren“.`;
  }
}

// Anteil am Auto-Akku in %, nach Abzug der Ladeverluste
function carPercent(kwh) {
  return ((kwh * (1 - settings.chargeLossPct / 100)) / settings.carBatteryKwh) * 100;
}

// WMO-Wettercodes von Open-Meteo
const WEATHER = {
  0: ['☀️', 'Klar'], 1: ['🌤️', 'Überwiegend klar'], 2: ['⛅', 'Teilweise bewölkt'], 3: ['☁️', 'Bedeckt'],
  45: ['🌫️', 'Nebel'], 48: ['🌫️', 'Reifnebel'],
  51: ['🌦️', 'Leichter Niesel'], 53: ['🌦️', 'Niesel'], 55: ['🌧️', 'Starker Niesel'],
  56: ['🌧️', 'Gefrierender Niesel'], 57: ['🌧️', 'Gefrierender Niesel'],
  61: ['🌧️', 'Leichter Regen'], 63: ['🌧️', 'Regen'], 65: ['🌧️', 'Starker Regen'],
  66: ['🌧️', 'Gefrierender Regen'], 67: ['🌧️', 'Gefrierender Regen'],
  71: ['🌨️', 'Leichter Schnee'], 73: ['🌨️', 'Schnee'], 75: ['❄️', 'Starker Schnee'], 77: ['🌨️', 'Schneegriesel'],
  80: ['🌦️', 'Leichte Schauer'], 81: ['🌧️', 'Schauer'], 82: ['⛈️', 'Starke Schauer'],
  85: ['🌨️', 'Schneeschauer'], 86: ['🌨️', 'Starke Schneeschauer'],
  95: ['⛈️', 'Gewitter'], 96: ['⛈️', 'Gewitter mit Hagel'], 99: ['⛈️', 'Gewitter mit Hagel'],
};

// Wahrscheinlicher Bereich als Text, z. B. "6–11"
function rangeText(d) {
  return d.range && d.pv >= 1 ? `${fmt(d.range[0], 0)}–${fmt(d.range[1], 0)}` : '';
}

function renderToday() {
  const d = shortDays.find((x) => x.lead === 0) ?? shortDays[0];
  if (!d) return;
  const w = d.weather;
  const [icon, text] = WEATHER[w.code] ?? ['❔', 'Unbekannt'];
  $('#todayDate').textContent = `Heute, ${WEEKDAYS[d.date.getDay()]} ${d.date.getDate()}.${d.date.getMonth() + 1}.`;
  const range = rangeText(d);
  const pvText = `${fmt(d.pv, 0)} kWh Sonnenstrom${range ? ` (${range})` : ''}`;
  $('#todayHeadline').textContent = d.best
    ? `${pvText} – fürs Auto ca. +${fmt(carPercent(d.car), 0)} % (${d.best.from}–${d.best.to} Uhr)`
    : `${pvText} – kein Überschuss fürs Auto`;
  $('#todaySnow').hidden = !d.snow;
  $('#todaySub').textContent = `${icon} ${text} · ${fmt(w.tMin, 0)}–${fmt(w.tMax, 0)} °C · ${fmt(w.sunHours)} h Sonne · ` +
    `Spitze ${fmt(d.peak)} kW${d.best ? ` · ${fmt(d.car, 0)} kWh fürs Auto` : ''}`;
  $('#todayNote').textContent = [
    range ? `In Klammern: wahrscheinlicher Bereich – 8 von 10 Tagen mit diesem Wetter lagen darin.` : '',
    d.hours.some((h) => h.past) ? 'Blasse Balken: bereits vergangene Stunden.' : '',
  ].filter(Boolean).join(' ');
  drawHourChart('todayChart', d);
}

function drawHourChart(canvasId, d) {
  const hours = d.hours.filter((h) => h.hour >= BAND_FIRST_HOUR && h.hour <= BAND_LAST_HOUR);
  // vergangene Stunden blasser darstellen
  const fade = (color) => hours.map((h) => (h.past ? color + '66' : color));
  const summary = d.best
    ? `Spitze ${fmt(d.peak)} kW, Auto ${d.best.from} bis ${d.best.to} Uhr, plus ${fmt(carPercent(d.car), 0)} Prozent`
    : `Spitze ${fmt(d.peak)} kW, kein Überschuss fürs Auto`;
  drawChart(canvasId, {
    type: 'bar',
    data: {
      labels: hours.map((h) => `${h.hour}`),
      datasets: [
        { label: 'Sonnenstrom', data: hours.map((h) => h.pv), backgroundColor: fade(cssVar('--sun')), ...barStyle },
        { label: 'Fürs Auto', data: hours.map((h) => h.car), backgroundColor: fade(cssVar('--charge')), ...barStyle },
      ],
    },
    options: {
      plugins: {
        tooltip: {
          callbacks: {
            title: (items) => `${items[0].label}–${Number(items[0].label) + 1} Uhr`,
            label: (c) => `${c.dataset.label}: ${fmt(c.parsed.y)} kW`,
          },
        },
      },
      scales: {
        x: { grid: { display: false }, title: { display: true, text: 'Uhr' }, ticks: { maxRotation: 0 } },
        y: { beginAtZero: true, title: { display: true, text: 'kW' } },
      },
    },
  }, `Stundenverlauf ${dayLabel(d)}: ${summary}`);
}

// Sonnenband: eine Zelle pro Stunde, Füllhöhe = PV-Leistung relativ zur stärksten Stunde der 8 Tage,
// grün = Stunde mit Überschuss fürs Auto
function bandHtml(d) {
  const scale = Math.max(...shortDays.map((x) => x.peak), 0.1);
  let html = '';
  for (let hr = BAND_FIRST_HOUR; hr <= BAND_LAST_HOUR; hr++) {
    const h = d.hours.find((x) => x.hour === hr);
    const level = h ? Math.min(h.pv / scale, 1) : 0;
    const car = h?.car > 0;
    html += `<span class="${car ? 'car' : ''}"><i style="top:${(1 - level) * 100}%"></i></span>`;
  }
  return `<div class="band" aria-hidden="true">${html}</div>`;
}

function renderDayList() {
  $('#dayList').innerHTML = shortDays.map((d) => {
    const w = d.weather;
    const [icon, text] = WEATHER[w.code] ?? ['❔', 'Unbekannt'];
    const carText = d.best ? ` · <span class="charge">Auto +${fmt(carPercent(d.car), 0)} %</span>` : '';
    const snowText = d.snow ? '<span class="snow">❄️ Schnee möglich</span> · ' : '';
    const range = rangeText(d);
    const label = `${dayLabel(d)}: ${text}, ${fmt(w.tMin, 0)} bis ${fmt(w.tMax, 0)} Grad, ${fmt(w.sunHours)} Sonnenstunden, ` +
      `${fmt(d.pv, 0)} Kilowattstunden Ertrag${range ? `, wahrscheinlich ${range}` : ''}${d.snow ? ', Schnee auf den Modulen möglich' : ''}${d.best ? `, Auto laden ${d.best.from} bis ${d.best.to} Uhr, plus ${fmt(carPercent(d.car), 0)} Prozent` : ''}. Prognose-Sicherheit ${d.reliability}.`;
    const detail = (range ? `Wahrscheinlich ${range} kWh · ` : '') + (d.best
      ? `Auto ${d.best.from}–${d.best.to} Uhr: ${fmt(d.car, 0)} kWh ≈ +${fmt(carPercent(d.car), 0)} % Akku · Spitze ${fmt(d.peak)} kW · Prognose-Sicherheit ${d.reliability}`
      : `Kein Überschuss fürs Auto · Spitze ${fmt(d.peak)} kW · Prognose-Sicherheit ${d.reliability}`);
    return `<div class="day-item${d.key === openKey ? ' open' : ''}" data-key="${d.key}">
      <button class="day" aria-expanded="${d.key === openKey}" aria-controls="detail-${d.key}" aria-label="${label}">
        <span class="day-name">${dayLabel(d)}<small>${fmt(w.tMin, 0)}° / ${fmt(w.tMax, 0)}°</small></span>
        <span class="day-icon" title="${text}">${icon}</span>
        ${bandHtml(d)}
        <span class="day-kwh">${fmt(d.pv, 0)} kWh${range ? `<small>${range}</small>` : ''}</span>
        <span class="day-meta">${snowText}${text} · ${fmt(w.sunHours)} h Sonne · ${fmt(w.precip)} mm${carText}</span>
      </button>
      <div class="day-detail" id="detail-${d.key}" ${d.key === openKey ? '' : 'hidden'}>
        <p class="chart-summary">${detail}</p>
        <div class="chart-box small"><canvas id="chart-${d.key}" role="img"></canvas></div>
      </div>
    </div>`;
  }).join('');
  document.querySelectorAll('.day-item .day').forEach((btn) =>
    btn.addEventListener('click', () => toggleDay(btn.parentElement.dataset.key)));
  if (openKey) {
    const d = shortDays.find((x) => x.key === openKey);
    if (d) drawHourChart(`chart-${openKey}`, d); else openKey = null;
  }
}

// Klappt das Stundendiagramm direkt unter dem angetippten Tag auf; nur ein Tag ist offen.
function toggleDay(key) {
  const wasOpen = openKey === key;
  if (openKey) {
    charts[`chart-${openKey}`]?.destroy();
    delete charts[`chart-${openKey}`];
  }
  openKey = wasOpen ? null : key;
  document.querySelectorAll('.day-item').forEach((item) => {
    const open = item.dataset.key === openKey;
    item.classList.toggle('open', open);
    item.querySelector('.day').setAttribute('aria-expanded', open);
    item.querySelector('.day-detail').hidden = !open;
  });
  if (openKey) drawHourChart(`chart-${openKey}`, shortDays.find((x) => x.key === openKey));
}

$('#refreshShort').addEventListener('click', runShort);

// ---------- Zeitraum ----------
let monthsLoaded = false;
let lastRange = null;

async function getHistory() {
  const status = $('#longStatus');
  status.className = 'status';
  try {
    const hist = await PV.history(settings, (msg) => (status.textContent = msg));
    status.textContent = '';
    return hist;
  } catch (err) {
    status.className = 'status error';
    status.textContent = `Die Wetterdaten konnten nicht geladen werden (${err.message}). Prüfe die Internetverbindung und versuche es erneut.`;
    throw err;
  }
}

async function loadMonths() {
  if (monthsLoaded) return;
  const hist = await getHistory();
  monthsLoaded = true;
  const months = PV.monthStats(hist);
  const names = ['Jan', 'Feb', 'Mär', 'Apr', 'Mai', 'Jun', 'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez'];
  const year = PV.rangeStats(hist, new Date(2023, 0, 1), new Date(2023, 11, 31));
  const summary = `Im Jahr typisch ${fmt(year.pv.median, 0)} kWh (${fmt(year.pv.median / settings.kwp, 0)} kWh/kWp) · schwächstes Jahr ${fmt(year.pv.min, 0)}, stärkstes ${fmt(year.pv.max, 0)} kWh`;
  $('#yearTotal').textContent = summary;
  drawChart('monthChart', {
    type: 'bar',
    data: {
      labels: names,
      datasets: [
        { label: 'Mittleres Jahr', data: months.map((m) => m.median), backgroundColor: cssVar('--sun'), order: 2, ...barStyle },
        { label: 'Spanne 10–90 %', data: months.map((m) => [m.p10, m.p90]), backgroundColor: cssVar('--label'), barPercentage: 0.12, grouped: false, order: 1, borderRadius: 2, borderSkipped: false },
      ],
    },
    options: {
      plugins: {
        tooltip: {
          callbacks: {
            label: (c) => Array.isArray(c.raw)
              ? `Spanne: ${fmt(c.raw[0], 0)}–${fmt(c.raw[1], 0)} kWh`
              : `Mittleres Jahr: ${fmt(c.parsed.y, 0)} kWh`,
          },
        },
      },
      scales: { x: { grid: { display: false }, ticks: { maxRotation: 0 } }, y: { beginAtZero: true, title: { display: true, text: 'kWh' } } },
    },
  }, `Ertrag pro Monat. ${summary}`);
}

function setRange(from, to) {
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  $('#rangeFrom').value = iso(from);
  $('#rangeTo').value = iso(to);
}

document.querySelectorAll('[data-quick]').forEach((btn) => btn.addEventListener('click', () => {
  const now = new Date();
  let from, to;
  if (btn.dataset.quick === 'month') {
    from = new Date(now.getFullYear(), now.getMonth() + 1, 1);
    to = new Date(now.getFullYear(), now.getMonth() + 2, 0);
  } else {
    const months = btn.dataset.quick === '3months' ? 3 : 12;
    from = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
    to = new Date(now.getFullYear(), now.getMonth() + months, now.getDate());
  }
  setRange(from, to);
  calcRange();
}));
$('#calcRange').addEventListener('click', calcRange);

async function calcRange() {
  const [fy, fm, fd] = $('#rangeFrom').value.split('-').map(Number);
  const [ty, tm, td] = $('#rangeTo').value.split('-').map(Number);
  const from = new Date(fy, fm - 1, fd), to = new Date(ty, tm - 1, td);
  if (!(from <= to)) {
    $('#longStatus').className = 'status error';
    $('#longStatus').textContent = 'Wähle ein Startdatum, das vor dem Enddatum liegt.';
    return;
  }
  lastRange = [from, to];
  const hist = await getHistory();
  const r = PV.rangeStats(hist, from, to);
  $('#rangeResult').hidden = false;
  $('#tMedian').textContent = `${fmt(r.pv.median, 0)} kWh`;
  $('#tRange').textContent = `${fmt(r.pv.p10, 0)} / ${fmt(r.pv.p90, 0)} kWh`;
  $('#tCar').textContent = `${fmt(r.car.median, 0)} kWh`;
  $('#tCarRangeLabel').textContent = `Volle Akkuladungen (${fmt(settings.carBatteryKwh, 0)} kWh)`;
  $('#tCarRange').textContent = `≈ ${fmt(carPercent(r.car.median) / 100)}`;
  $('#tPerDay').textContent = `${fmt(r.pv.median / r.days)} kWh · ${r.days} Tage`;
  drawChart('yearChart', {
    type: 'bar',
    data: {
      labels: r.perYear.map((y) => y.year),
      datasets: [
        { label: 'Sonnenstrom', data: r.perYear.map((y) => y.pv), backgroundColor: cssVar('--sun'), ...barStyle },
        { label: 'Fürs Auto', data: r.perYear.map((y) => y.car), backgroundColor: cssVar('--charge'), ...barStyle },
      ],
    },
    options: { plugins: { tooltip: kwhTooltip }, scales: { x: { grid: { display: false } }, y: { beginAtZero: true, title: { display: true, text: 'kWh' } } } },
  }, `Ertrag im gewählten Zeitraum je Jahr 2015 bis 2024, Median ${fmt(r.pv.median, 0)} kWh`);
}

// Diagramme beim Wechsel zwischen hellem und dunklem Erscheinungsbild neu einfärben
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  if (shortDays.length) { renderToday(); renderDayList(); }
  if (monthsLoaded) { monthsLoaded = false; loadMonths(); }
  if (lastRange) calcRange();
});

// ---------- Start ----------
loadConfig().then(() => {
  fillForm();
  runShort();
});
