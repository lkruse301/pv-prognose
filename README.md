# PV Prognose

Ertragsprognose für die Freiland-PV-Anlage in Schling (Detmold-Heiligenkirchen) mit Empfehlung, wann das E-Auto mit Sonnenstrom geladen werden kann.

## Starten

- **Online (Handy, mehrere Personen):** Die Seite läuft kostenlos über GitHub Pages. Auf dem iPhone in Safari öffnen → Teilen → „Zum Home-Bildschirm“, dann startet sie wie eine App.
- **Lokal:** im Terminal in diesem Ordner starten und http://localhost:8765 öffnen:

```bash
python3 -m http.server 8765
```

Per Doppelklick auf `index.html` funktioniert die Seite auch, nutzt dann aber die Standardwerte aus `js/pv.js` statt `config.json`.

## Synchronisierung

Alle Anlagenwerte stehen in `config.json`. Seite und Morgenbericht lesen diese Datei, daher sehen alle Geräte und alle Personen dieselben Werte und dieselbe Prognose. Werte ändern: `config.json` auf GitHub bearbeiten, nach etwa einer Minute ist die Änderung überall aktiv.

## Morgenbericht aufs Handy

- Die Claude-Cloud-Routine „PV-Morgenbericht“ läuft jeden Morgen um 6:30 Uhr auf den Servern von Anthropic, es muss also kein eigenes Gerät laufen.
- Sie lädt dieses Repository, führt `morgenbericht.py` aus und schickt das Ergebnis als Push-Benachrichtigung über die Claude-App.
- Die Cloud-Umgebung der Routine muss `api.open-meteo.com` erreichen dürfen (Netzwerkfreigabe der Umgebung).
- Den Bericht bekommt nur das Claude-Konto, dem die Routine gehört.
- Test ohne Routine:

```bash
python3 morgenbericht.py
```

## Ansichten

- **Nächste Tage:** Wetterbericht und stündliche Ertragsprognose für heute und die nächsten 7 Tage. Die App zeigt, in welchen Stunden Überschuss fürs Auto da ist und wie viel Prozent des 60-kWh-Auto-Akkus das jeweils ausmacht (nach Ladeverlusten).
- **Zeitraum:** Für beliebige Zeiträume in der Zukunft. Das ist keine Wetterprognose, sondern eine Statistik: Was hätte die Anlage im selben Kalenderzeitraum in den Jahren 2015–2024 erzeugt (Median, schwaches und starkes Jahr)?
- **Anlage:** Anlagen-, Speicher-, Wallbox- und Auto-Daten aus `config.json` (nur Anzeige).

## Modell

- Einstrahlung auf die geneigte Modulfläche direkt von Open-Meteo (Vorhersage bzw. Archiv)
- Bifaziale Rückseite: Bodenreflexion und diffuses Licht, Bifazialität 0,7, Abschattungsfaktor 0,6
- Zelltemperatur nach Faiman, Temperaturkoeffizient −0,35 %/K
- 14 % Systemverluste, Begrenzung auf die Wechselrichterleistung (10 kW)
- Stündliche Simulation: Hausverbrauch (gleichmäßig verteilt) → Akku → Überschuss ab Mindestladeleistung fürs Auto

## Standardwerte (in config.json anpassbar)

| Wert | Annahme |
|---|---|
| Standort | 51,9045 N / 8,8563 E (Ortsmitte Schling) |
| Ausrichtung / Neigung | Süd / 35° (ertragsoptimal für den Standort) |
| Anlage | 9,0 kWp (20 × 450 W), Wechselrichter 10 kW |
| Speicher | 10 kWh, 5 kW |
| Wallbox | dreiphasig 4,1–11 kW, bei weniger Überschuss automatisch einphasig 1,4–3,7 kW |
| Hausverbrauch | 6 kWh/Tag |
| E-Auto | 60 kWh Akku, 10 % Ladeverluste |

## Nächste Schritte

- Huawei-FusionSolar-API anbinden (Zugang prüfen), um mit echten Ertragsdaten die Verluste zu kalibrieren
