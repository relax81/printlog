# printlog (Deutsch)

*English: [README.md](../README.md)*

Stromkosten pro 3D-Druck, gespeist aus Home Assistant.

Eine HA-Automation pro Drucker (Blueprint „printlog: Druck erfassen“) meldet jeden Druck als Event
`printlog`. Dieser Container hört per HA-WebSocket zu, speichert jeden Druck in SQLite und holt
das Vorschaubild aus HA. Er spricht nur mit Home Assistant, nie mit der Bambu-Cloud.

## Ablauf pro Druck

| Event-Phase | Wann | Was passiert |
|---|---|---|
| `start` | Status wechselt auf `running` (nicht aus Pause/Offline) | Zeile anlegen, Zählerstand merken, Vorschaubild holen |
| `stop`  | Status wechselt von `running`/`pause`/`prepare` auf `finish`/`failed` | Status fertig/abgebrochen, Zählerstand und Fortschritt merken |
| `final` | Leistung < 30 W (Abkühlen vorbei, max. 5 min) + 70 s | kWh = Ende − Start, Kosten = kWh × `input_number.strompreis` |

Sonderfälle:

- Container war aus (Redeploy, Neustart) oder die Verbindung zu HA weg: Nach jedem Verbinden liest der Container den Verlauf der Druckstatus- und Zähler-Sensoren aus dem HA-Recorder (seit der letzten Verbindung, max. 7 Tage) und holt verpasste `start`/`stop` nach denselben Regeln wie die Automation nach, mit dem Zählerstand zum Zeitpunkt des Übergangs. Liegt das Ende länger als die Abkühlphase zurück, gilt der Zählerstand 6,5 min nach dem Ende. Fehlende Vorschaubilder offener Drucke werden neu geholt. Bereits erfasste Events werden nicht doppelt angelegt.
- Kein `final` innerhalb von 20 min und auch nichts nachzuholen: Abschluss mit dem Zählerstand vom `stop`.
- Neuer Start ohne Ende davor: alte Zeile wird „unvollständig“.
- Ende ohne Start (Start auch im Verlauf nicht zu finden): Zeile „unvollständig“ ohne kWh.
- Plug offline oder Zähler zurückgesetzt: keine kWh, Hinweis in der Zeile.
- Fehldrucke bekommen eine eigene Zeile „abgebrochen“ mit den Kosten bis zum Abbruch.

## Kosten pro Druck

- **Strom:** kWh × `input_number.strompreis` zum Druckende.
- **Drucker:** Druckdauer × (Anschaffung ÷ Lebensdauer in h + Wartung €/Jahr ÷ Druckstunden/Jahr + Verschleiß €/h), pro Drucker unter *Einstellungen*.
- **Filament:** Gramm pro AMS-Slot × €/kg. Preis nach Sorte (z. B. „Bambu PLA Matte“), sonst nach Typ (PLA, PETG …).
  Bei Abbruch nur der Anteil bis zum Abbruch.
- Nach dem Speichern der Einstellungen werden alle Drucke mit den aktuellen Preisen neu berechnet.

Das Material kommt pro Slot aus den `print_weight`-Attributen und den AMS-Slot-Sensoren in HA
(Feld `materials` im start/stop-Event). Ohne Aufteilung: aktiver Slot mit Gesamtgewicht.

## Oberfläche

* **Drucke**: Live-Suche (Teilwörter, mehrere Wörter, Umlaute egal) über Name, Drucker, Material, Kunde und Notiz, dazu Filter. Umschalten zwischen Tabelle und Galerie, optional nach Tagen gruppiert (Schalter „Nach Tagen“, merkt sich der Browser). In der Tabelle zeigt ein Farbbalken die Kostenanteile; Antippen (Tablet) oder Drüberfahren (Desktop) öffnet die Aufschlüsselung. Ein Klick auf eine Zeile öffnet die Detailseite. CSV-Export (Excel, Semikolon) mit den aktuellen Filtern.
* **Druckdetails** (`job?id=N`): Vorschaubild, alle Werte, Name/Kunde/Stückzahl/Ausschuss/Notiz bearbeiten, Druck löschen (Knopf oben rechts und im Tab „Bearbeiten“, landet im Papierkorb), eigene Fotos hochladen (mehrere, bis 20 MB, am Handy auch direkt mit der Kamera).
* **Statistik**: Tag/Woche/Monat mit Druckstunden pro Drucker, Kosten (Strom/Filament/Drucker), Filament nach Material, Tabelle pro Drucker und Jahres-Heatmap.
* **Kalender**: Monatsansicht mit Drucken, Stunden, Kosten und Vorschaubildern pro Tag.
* **Kunden**: anlegen, bearbeiten, löschen (Drucke bleiben, nur ohne Kunde). Pro Kunde: offener Betrag, bezahlter Betrag und nicht übergebene Drucke als farbige Schilder (Klick zeigt die Drucke), Filter „Nur mit Offenem“.
* **Endpreis, Zahlung, Übergabe** (nur Drucke mit Kunde, ohne Ausschuss): Optionaler Endpreis pro Druck, sonst gilt Kosten + Kundenmarge. Zahlungen und Übergaben sind einzelne Einträge (Betrag bzw. Stückzahl, Datum, Notiz), damit Teilzahlungen und Teilabholungen gehen. In der Liste zeigen Badges „nicht bezahlt / 2,00/5,00 € / bezahlt“ und „nicht übergeben / 2/4 übergeben / übergeben“; Antippen öffnet ein Menü (z. B. „Alles bezahlt“). Auf der Detailseite steht alles im Tab „Abrechnung“ (nur mit Kunde), die Übersicht zeigt eine kompakte Zeile mit Betrag und Badges. Filter „Zahlung/Übergabe“ in der Liste, Spalten im CSV-Export.
* **Verschenkt**: Ein Druck lässt sich als verschenkt markieren (Tab „Abrechnung“, Tab „Bearbeiten“ oder im Menü des Zahlungs-Badges). Er zählt weiter in Kosten und Statistik, braucht aber keine Zahlung, taucht nicht als offen auf (Liste, Kunden) und bekommt das Badge „🎁 verschenkt“. Filter „verschenkt“ in der Liste, im CSV steht „Zu zahlen“ dann auf 0.
* **Teilen** (Knopf „📤 Teilen“ oben auf der Detailseite): „Bild für Kunden“ ist ein PNG im Kassenbon-Stil mit Foto, Titel, Stückzahl, Preis, Zahlungs- und Übergabestand (Stempel „BEZAHLT“ bzw. „GESCHENK“), ohne Kalkulation. „Bild intern“ zeigt zusätzlich Druckdaten, Material, Kosten, Margen und Gewinn. „PDF“ ist eine A4-Kostenaufstellung mit voller Aufschlüsselung, Zahlungen und Übergaben zum Ablegen. Am Handy/Tablet öffnet sich das Teilen-Menü des Systems (z. B. WhatsApp), am PC wird die Datei heruntergeladen. Das Häkchen „Bezahlt/Übergeben mit drauf“ im Menü (merkt sich der Browser) lässt Zahlungs- und Übergabestand bei allen Exporten weg (`?status=0`). Im Diskret-Modus bietet das Menü nur das Kundenbild an. Gerendert wird im Container (Pillow, ReportLab, Schrift DejaVu), direkt abrufbar unter `export/<id>/bon-kunde.png`, `bon-intern.png` und `kosten.pdf`. Als Preis gilt der Endpreis, sonst Kosten + Kundenmarge, ohne Kunde die erste allgemeine Marge.
* **Filament-Notizen**: Im Tab „Bearbeiten“ lässt sich pro Material-Zeile eines Drucks eine Notiz hinterlegen, z. B. der genaue Farbname („Bambu PLA Matte Ivory White“). Sie steht in der Übersicht unter dem Filament, auf dem Bon (für Kunden als Abschnitt „Farben“, intern statt des Namens) und im PDF, und sie ist durchsuchbar.
* **Mehrfachauswahl**: „☑ Auswählen“ in der Druckliste, dann Drucke antippen (Tabelle, Galerie, Handy) und unten „🗑 Löschen“. Danach gibt es kurz „Rückgängig“.
* **Papierkorb** (`trash`, verlinkt unter Einstellungen): Gelöschte Drucke lassen sich wiederherstellen und werden erst nach einstellbaren Tagen (Standard 30) samt Fotos, Zahlungen und Übergaben endgültig gelöscht. Das Nachholen verpasster Events nach einem Redeploy legt Drucke aus dem Papierkorb nicht neu an.
* **Verschenktes pro Kunde**: Unter „Kunden“ zeigt die Spalte „Verschenkt“ Anzahl und Wert der verschenkten Drucke, mit Zeitraum (Monat, Jahr, frei) und Filter „Mit Verschenktem“.
* **Diskret-Modus**: Das Auge oben rechts verwischt alle Kalkulationswerte (Kosten, Margen, Statistik) auf allen Seiten, z. B. wenn ein Kunde mitschaut. Sichtbar bleiben Endpreis bzw. Kundenpreis und der Zahlungs-/Übergabestatus. Der Browser merkt sich die Einstellung.
* **Einstellungen**: in Tabs gegliedert (Kosten, Filament, Margen, Bon & PDF, Ansicht, Backup, Windows), am Handy wischbar und oben fixiert. Ein „Speichern“ unten gilt für alle Tabs, danach öffnet sich wieder der zuletzt benutzte Tab.
* **Eigenes Bon-Logo** (Einstellungen → „Bon & PDF“): PNG/JPG/WebP hochladen, ersetzt das App-Logo oben auf dem Bon-Bild und im PDF-Kopf. Optional wird ein weißer Hintergrund durchsichtig gemacht, durchsichtige Ränder werden abgeschnitten. „Zurück zum App-Logo“ stellt den Standard wieder her. Gespeichert als `images/bon_logo_<zeit>.png` im Datenvolume.
* **Projekt-ID**: Jeder Druck hat eine kurze ID wie `PL-7K3Q` (4 Zeichen aus 31 ohne Verwechsler 0/O, 1/I/L, rund 920 000 Kombinationen, zufällig und eindeutig, auch gegenüber dem Papierkorb). Ältere Drucke bekommen sie beim ersten Start automatisch. Sie steht in der Liste, auf der Detailseite (Klick kopiert), auf Bon, Barcode, PDF und im Dateinamen. Suchen geht mit und ohne „PL-“, Groß/klein egal; Enter auf einer gültigen ID öffnet den Druck direkt (`go?q=7k3q`).

* **Ausschuss**: Abgebrochene Drucke zählen automatisch als Ausschuss. Auf der Detailseite (Tab „Bearbeiten“) lässt sich das pro Druck ändern. Filter „nur Ausschuss“ und Kennzahl mit Kosten.
* **Margen**: Zwei allgemeine Aufschläge (Einstellungen) als eigene Spalten, dazu optional eine Marge pro Kunde.
* **Projektlink**: MakerWorld-/Printables-Link pro Druck. Bleibt der Titel leer, wird er aus der Seite gelesen (oder aus dem Link gebildet). Titel und Link sind durchsuchbar.
* **Lokaler Ordner**: Windows-Pfad pro Druck (Ordner oder Datei, z. B. für Projekte, die nur auf dem PC liegen), mit „Kopieren“ und „Öffnen“. „Öffnen“ nutzt das Link-Protokoll `printlog-open:`, das einmalig pro PC per Doppelklick auf `printlog-einrichten.cmd` eingerichtet wird (Download und Anleitung unter Einstellungen → Windows). Ordner öffnen sich im Explorer. 3D-Dateien (3MF, STL, STEP, OBJ) öffnen sich im Standardprogramm, aber nur innerhalb des Projektordners, den man bei der Einrichtung am PC wählt. Alles andere wird nur im Explorer markiert, Programme oder Skripte startet ein Link nie. Durchsuchbar und im CSV-Export.
* Eigene Fotos werden beim Hochladen gedreht (EXIF) und auf höchstens 2560 px verkleinert (JPEG, Qualität 85).

## Drucker und Home Assistant

Unter *Einstellungen → Drucker* steht pro Drucker, welche HA-Entitäten dazugehören: Druckstatus-Sensor, Energiezähler (kWh),
AMS-Einheiten, externe Spule und der Namensanfang der übrigen Bambu-Entitäten (Auftrag, Gewicht, Fortschritt, Vorschaubild).
Ein ✓ zeigt den aktuellen Wert aus HA, ⚠ eine Entität, die es nicht gibt. Gespeichert wird das als Einstellung `printers` in der Datenbank.

* **Ohne Steckdose:** Energiezähler leer lassen und eine *feste Leistung* in W eintragen. printlog rechnet dann kWh = Druckdauer × Leistung
  (Hinweis „kWh geschätzt mit … W“ in der Zeile). Fällt eine vorhandene Steckdose aus, gilt derselbe Ersatzwert.
* **Übernahme:** Bei einer Installation von vor der Drucker-Zuordnung übernimmt printlog beim ersten Start die Drucker aus den vorhandenen Drucken (Energiezähler optional per Env `ENERGY_ENTITY` mit Platzhalter `{printer}`). Eine neue Installation ohne Drucke startet leer.
* **Lernen aus Events:** Schickt die Automation die Entitäten im Event mit (Blueprint, siehe unten), übernimmt printlog sie und legt
  unbekannte Drucker selbst an. Name und feste Leistung bleiben, wie sie in den Einstellungen stehen.

### Blueprint „printlog: Druck erfassen (Bambu Lab)“

`../blueprints/automation/printlog/printlog_drucker.yaml` ersetzt die bisherige Sammel-Automation durch eine Automation pro Drucker.
Ausgewählt werden Kurzname, Druckstatus, AMS-Einheiten (jeweils der Sensor von Fach 1), externe Spule und optional Energiezähler,
Leistung und Strompreis. Unter „Feinheiten“: Standby-Leistung (30 W), Abkühlphase höchstens (5 min), Nachlauf für den Zähler (70 s).
Ohne Strompreis-Entität fragt printlog selbst `PRICE_ENTITY` ab. Ohne Energiezähler kommt das Ende-Event sofort.

Installieren: Die Datei nach `/config/blueprints/automation/printlog/` kopieren (oder bei öffentlichem Repo über
*Blueprint importieren* mit der GitHub-Adresse), dann unter *Einstellungen → Automationen → Blueprints* pro Drucker eine Automation anlegen.
Wichtig: Für bestehende Drucker denselben Kurznamen nehmen und die alte Sammel-Automation erst danach ausschalten, sonst kommen Events doppelt.

Benötigt: Bambu-Lab-Integration (ha-bambulab), optional eine Steckdose mit Energie- und Leistungssensor pro Drucker,
ein Long-Lived Access Token für printlog.

Vorschaubilder und Fotos liegen im Volume unter `/data/images`, die Datenbank unter `/data/printlog.db`.

## Backup

Mit `BACKUP_DIR=/backup` und einem eingehängten Netzwerkordner sichert der Container eine Kopie der Datenbank (`db/printlog-JJJJ-MM-TT.db`, die letzten 14 bleiben) und spiegelt alle Bilder nach `images/`. Wie oft, legst du in den Einstellungen fest: täglich oder wöchentlich (Standard: wöchentlich, sonntags um 3 Uhr), dazu Wochentag und Uhrzeit. Änderungen greifen ohne Neustart. `BACKUP_HOUR` gibt nur die Standard-Uhrzeit vor. In den Einstellungen gibt es außerdem „Jetzt sichern“, das nächste geplante und das letzte Backup.

## Später

* HA-Sensoren für Monatskosten und Druckstunden
* Filamentlager über die Filametrix-API, sobald sie offiziell ist

## Installation

Siehe die englische [README](../README.md#installation). Kurz: `.env.example` nach `.env` kopieren, `HA_URL` und `HA_TOKEN` setzen,
`docker compose up -d --build`. In Portainer (Git-Stack) dieselben Variablen unter „Environment variables“ eintragen.
Die Seite läuft auf Port 8095 (`PRINTLOG_PORT`), Status unter `/health`. Daten liegen im Volume `printlog-data` (`printlog.db` + `images/`).

## API

* `POST /api/jobs/{id}` (Formular: task_name, customer_id, quantity, job_note)
* `POST /api/jobs/{id}/photos` (multipart, Feld `photo`)
* `GET /export.csv`

- `GET /` Tabelle mit Filtern (`?printer=h2s&month=2026-11&status=fertig&material=PLA`)
- `GET /settings` Preise für Drucker und Filament
- `GET /api/jobs` Zeilen als JSON (gleiche Filter, `limit`, `offset`)
- `GET /api/summary` Summen gesamt und pro Drucker
- `POST /api/jobs/{id}/delete` Druck in den Papierkorb
- `POST /api/jobs/delete` mehrere in den Papierkorb (`ids=1,2,3`)
- `POST /trash/restore` (`ids`), `POST /trash/purge` (`ids` oder `all=1`)
- `GET /health` 200 wenn mit HA verbunden, sonst 503
