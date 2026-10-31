# PV Prognose

Ertragsprognose für die Freiland-PV-Anlage mit Empfehlung, wann das E-Auto mit Sonnenstrom geladen werden kann.

## Starten

- **Online (Handy, mehrere Personen):** https://lkruse301.github.io/pv-prognose/ – kostenlos über GitHub Pages. Auf dem iPhone in Safari öffnen → Teilen → „Zum Home-Bildschirm“, dann startet sie wie eine App.
- **Lokal:** im Terminal in diesem Ordner starten und http://localhost:8765 öffnen:

```bash
python3 -m http.server 8765
```

Per Doppelklick auf `index.html` funktioniert die Seite auch, nutzt dann aber die Standardwerte aus `js/pv.js` statt `config.json`.

## Synchronisierung

Alle Anlagenwerte stehen in `config.json`. Seite und Morgenbericht lesen diese Datei, daher sehen alle Geräte und alle Personen dieselben Werte und dieselbe Prognose. Werte ändern: `config.json` auf GitHub bearbeiten, nach etwa einer Minute ist die Änderung überall aktiv.

## Morgenbericht aufs Handy

- Die Claude-Cloud-Routine „PV-Morgenbericht“ (https://claude.ai/code/routines/trig_018D6RLVRzfNeMKn4U982ESt) läuft jeden Morgen um 6:30 Uhr auf den Servern von Anthropic, es muss also kein eigenes Gerät laufen.
- Sie lädt dieses Repository, führt `morgenbericht.py` aus und schickt das Ergebnis als Push-Benachrichtigung über die Claude-App.
- Wetterdaten kommen von Open-Meteo. Ist dort das kostenlose Tageslimit erschöpft (Cloud-Server teilen sich IP-Adressen), nutzt das Skript die DWD-Vorhersage über Bright Sky und berechnet die Einstrahlung auf die Module selbst.
- Die Cloud-Umgebung „PV Prognose“ muss `api.open-meteo.com` und `api.brightsky.dev` erreichen dürfen.
- Den Bericht bekommt nur das Claude-Konto, dem die Routine gehört.
- Test ohne Routine:

```bash
python3 morgenbericht.py
```

## Ansichten

- **Prognose:** Oben der heutige Tag mit Stundendiagramm, darunter die Vorhersage für 8 Tage; ein Tipp auf einen Tag klappt dessen Stundenverlauf auf. Die App zeigt, in welchen Stunden Überschuss fürs Auto da ist und wie viel Prozent des 60-kWh-Auto-Akkus das jeweils ausmacht (nach Ladeverlusten).
- **Zeitraum:** Oben das ganze Jahr mit dem erwarteten Ertrag je Monat, darunter beliebige eigene Zeiträume. Das ist keine Wetterprognose, sondern eine Statistik: Was hätte die Anlage im selben Kalenderzeitraum in den Jahren 2015–2024 erzeugt (Median, schwaches und starkes Jahr)?
- **Anlage:** Anlagen-, Speicher-, Wallbox- und Auto-Daten aus `config.json` (nur Anzeige).

## Modell

- Einstrahlung auf die geneigte Modulfläche direkt von Open-Meteo (Vorhersage bzw. Archiv)
- Bifaziale Rückseite: Bodenreflexion und diffuses Licht, Bifazialität 0,7, Abschattungsfaktor 0,6
- Zelltemperatur nach Faiman, Temperaturkoeffizient −0,35 %/K
- 14 % Systemverluste, Begrenzung auf die Wechselrichterleistung (11 kW)
- Horizont-Abschattung durch das Gelände: Profil von PVGIS (EU-Forschungszentrum) in `config.json`; verdeckte Direktstrahlung wird abgezogen (Dezember/Januar etwa −8 %, aufs Jahr −0,6 %)
- Kalibrierfaktor je Monat aus echten Erträgen (`calibration` in `config.json`, siehe unten)
- Wetterabhängige Korrektur: Faktor a + b·Klarheit des Tages (vorhergesagte Globalstrahlung im Verhältnis zum wolkenlosen Himmel); trübe Tage wurden sonst über-, sonnige unterschätzt (`weatherCorrection`)
- Bandbreite: Bereich, in dem 8 von 10 Tagen mit ähnlichem Wetter lagen, getrennt nach trüb/wechselhaft/sonnig (`band`)
- Schneewarnung, wenn mehr als 1 cm Schneedecke gemeldet ist und es tagsüber unter 3 °C bleibt
- Stündliche Simulation: Hausverbrauch (gleichmäßig verteilt) → Akku → Überschuss ab Mindestladeleistung fürs Auto

## Standardwerte (in config.json anpassbar)

| Wert | Annahme |
|---|---|
| Standort | 51,9045 N / 8,8563 E (Ortsmitte Schling) |
| Ausrichtung / Neigung | Süd / 35° (ertragsoptimal für den Standort) |
| Anlage | 10,0 kWp, Wechselrichter max. 11 kW (beides laut FusionSolar) |
| Speicher | 10 kWh, 5 kW |
| Wallbox | dreiphasig 4,1–11 kW, bei weniger Überschuss automatisch einphasig 1,4–3,7 kW |
| Hausverbrauch | 7 kWh/Tag ohne Auto |
| E-Auto | 60 kWh Akku, 10 % Ladeverluste |

## Kalibrierung mit echten Erträgen

1. In FusionSolar die Tageserträge (oder Monatserträge) der Anlage als Excel- oder CSV-Datei exportieren – je länger der Zeitraum, desto besser, ideal ein ganzes Jahr.
2. Auswerten, ohne etwas zu ändern:

```bash
python3 kalibrieren.py Export.xlsx --dry-run
```

3. Passt die Auswertung, ohne `--dry-run` ausführen. Das Skript schreibt einen Faktor je Monat, die wetterabhängige Korrektur und die Bandbreiten in `config.json`; nach dem Hochladen auf GitHub nutzen App und Morgenbericht ihn automatisch.

Das Skript erkennt die Ertragsspalte selbst (z. B. „PV-Ertrag (kWh)“); sonst mit `--spalte "Spaltenname"` angeben. Tage mit Schnee oder Ausfall (weniger als 30 % des Modells) werden übersprungen.

## Nächste Schritte

- Huawei-FusionSolar-API anbinden (Zugang prüfen), um mit echten Ertragsdaten die Verluste zu kalibrieren
