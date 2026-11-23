# printlog

**Cost tracking for every 3D print, fed by Home Assistant.**

printlog is a small self-hosted web app that logs each print of your Bambu Lab printers automatically: when it ran,
what it printed, how much filament from which AMS slot, how much electricity it used, and what it cost. You add
customers, prices, payments and photos on top. It runs as a single Docker container next to Home Assistant and
never talks to the Bambu cloud.

*Deutsche Dokumentation: [docs/README.de.md](docs/README.de.md). The user interface is in German.*

---

## Features

- **Automatic print log** – start, end, status (finished / failed / incomplete), duration, task name and cover image of every print.
- **Real costs per print**
  - *Electricity* from a smart plug's kWh meter (start → end of cool-down), or an estimate from a fixed wattage if you have no plug.
  - *Filament* per AMS slot (grams × €/kg), priced by type (PLA, PETG …) or by exact product (e.g. "Bambu PLA Matte").
  - *Printer* depreciation, maintenance per year and wear per hour.
- **Customers and selling prices** – two named margins, an optional margin per customer, a fixed final price per print,
  partial payments and partial hand-overs, "gift" prints, open amounts per customer.
- **Sharing** – a receipt-style PNG for the customer (WhatsApp etc.), an internal version with full calculation, and an A4 PDF cost sheet.
- **Organising** – live search, filters, table / gallery / grouped by day, own photos (camera upload, auto-resized),
  MakerWorld / Printables links, a short project ID per print (e.g. `PL-7K3Q`), scrap marking, trash bin with restore.
- **Statistics** – day / week / month, hours per printer, cost split, filament by material, yearly heat map, calendar view.
- **Export and backup** – CSV export (Excel-friendly), scheduled backup of database and images to a mounted folder.
- **Discreet mode** – one click blurs all internal figures when a customer is watching.
- **Robust** – after a restart or lost connection, printlog replays missed prints from Home Assistant's history (up to 7 days).

## How it works

```
 Bambu printer ──► Home Assistant ──(event "printlog")──► printlog container ──► SQLite + images
                    │  Bambu Lab integration                 │  web UI on port 8095
                    │  smart plug (optional)                 │  reads states/history/images via HA API
                    └─ one automation per printer (blueprint)
```

1. The **Bambu Lab integration** in Home Assistant exposes print status, task name, weight per AMS slot and the cover image.
2. An **automation per printer** (from the included blueprint) fires a `printlog` event at print start, at print end,
   and once more after the cool-down phase with the final meter reading.
3. The **printlog container** listens on Home Assistant's WebSocket API, stores each print and calculates its costs.

Home Assistant stays lean: it only sends events. All logic and evaluation happen in the container.

## Requirements

| What | Notes |
|---|---|
| Home Assistant | Recent version (blueprint needs 2024.10 or newer). |
| [Bambu Lab integration](https://github.com/greghesp/ha-bambulab) | Installed via HACS. Provides the `…_print_status`, `…_print_weight`, `…_task_name`, AMS tray sensors and `image.…_cover_image`. |
| Smart plug per printer *(optional)* | Needs an **energy** sensor (kWh, total) and ideally a **power** sensor (W). Shelly, Tasmota, Zigbee plugs etc. all work. Without a plug, set a fixed wattage per printer in printlog. |
| Electricity price | An entity holding €/kWh, e.g. a number helper `input_number.strompreis`. |
| Docker | Docker Compose or Portainer on any host that can reach Home Assistant. |

Other printer brands are not supported out of the box (see [Other printers](#other-printers)).

## Installation

### 1. Prepare Home Assistant

1. Install the **Bambu Lab** integration via HACS and add your printers.
2. *(Optional)* Add a smart plug for each printer.
3. Create the electricity price helper: *Settings → Devices & services → Helpers → Create helper → Number*,
   name it e.g. "Strompreis" (entity `input_number.strompreis`), unit €/kWh, step 0.001, and set your price.
4. Create a **long-lived access token**: click your user profile → *Security* → *Long-lived access tokens* → *Create token*.
   Name it `printlog` and copy it.

### 2. Run the container

**Docker Compose**

```bash
git clone https://github.com/relax81/printlog.git
cd printlog
cp .env.example .env      # set HA_URL and HA_TOKEN
docker compose up -d --build
```

Open `http://<docker-host>:8095`. `http://<docker-host>:8095/health` returns `200` once printlog is connected to Home Assistant.

**Portainer (Git stack)**

*Stacks → Add stack → Repository*: repository URL of this repo, branch `main`, compose path `docker-compose.yml`.
Under *Environment variables* add at least `HA_URL` and `HA_TOKEN`. Deploy. To update later: *Pull and redeploy*
(or enable GitOps polling).

### 3. Install the blueprint

The blueprint lives at [`blueprints/automation/printlog/printlog_drucker.yaml`](blueprints/automation/printlog/printlog_drucker.yaml).

- **Public repo or your own copy:** *Settings → Automations & scenes → Blueprints → Import blueprint* and paste the file's GitHub URL.
- **Private repo:** copy the file to `/config/blueprints/automation/printlog/printlog_drucker.yaml` on your Home Assistant
  (File editor or Samba app), then reload automations under *Developer tools → YAML* (called *Tools* in newer versions) or restart Home Assistant.

### 4. Create one automation per printer

*Settings → Automations & scenes → Blueprints → "printlog: Druck erfassen (Bambu Lab)" → Create automation*, then fill in:

| Input | What to pick |
|---|---|
| **Kurzname** (short name) | Short unique key, lowercase, e.g. `x1c`. Prints are stored under this key, so don't change it later. |
| **Druckstatus** (print status) | The printer's `sensor.…_print_status`. All other printer entities are found by the same name prefix. |
| **AMS-Einheiten** (AMS units) | For each AMS, its *tray 1* sensor (`sensor.…_tray_1`), in order AMS 1, AMS 2 … Leave empty without AMS. |
| **Externe Spule** (external spool) | `sensor.…_external_spool` *(optional)* |
| **Energiezähler** (energy meter) | The plug's kWh sensor *(optional)* |
| **Leistung** (power) | The plug's W sensor *(optional, used to detect the end of the cool-down)* |
| **Strompreis** (price) | e.g. `input_number.strompreis` *(optional, otherwise printlog reads `PRICE_ENTITY` itself)* |
| *Feinheiten* (tuning) | Standby threshold (30 W), max cool-down (5 min), meter delay (70 s). Defaults fit Shelly plugs. |

Repeat for every printer. Start a print: it shows up in printlog within a minute, the cover image follows shortly after.

### 5. Finish the setup in printlog

Open **Einstellungen** (settings):

- **Drucker** – printlog fills this in from the first event of each automation. A ✓ shows the live value from Home Assistant,
  ⚠ an entity that does not exist. Set **Feste Leistung (W)** for printers without a plug. You can also add printers by hand.
- **Kosten** – purchase price, expected lifetime in print hours, maintenance per year and wear per hour for each printer.
- **Filament** – €/kg per filament type, optionally per exact product.
- **Margen** – up to two named margins (e.g. private / business).
- **Bon & PDF** – your own logo for the receipt image and PDF.
- **Backup** – schedule (needs `BACKUP_DIR`, see below).

### 6. Optional extras

- **Reverse proxy** (Nginx Proxy Manager, Traefik, Caddy …): point a hostname to `<docker-host>:8095` and enable WebSocket support.
- **Home Assistant dashboard**: embed printlog with a *Webpage* card (`type: iframe`) – use the HTTPS URL if HA itself runs on HTTPS.
- **Windows folder links**: each print can store a local folder or file path. *Einstellungen → Windows* offers a double-click
  setup (`printlog-einrichten.cmd`) that registers a `printlog-open:` link handler for the current user. Folders open in Explorer;
  3D files (3MF, STL, STEP, OBJ) open in their default app only if they lie inside a project folder chosen on the PC during setup,
  everything else is just highlighted in Explorer. A link never starts programs or scripts.

## Configuration

All settings are environment variables (in `.env` or the Portainer stack). Everything else is configured in the web UI.

| Variable | Default | Purpose |
|---|---|---|
| `HA_URL` | `http://homeassistant.local:8123` | Home Assistant address as reachable from the container. |
| `HA_TOKEN` | – (required) | Long-lived access token. |
| `PRICE_ENTITY` | `input_number.strompreis` | Price per kWh, read when an event carries no price. |
| `TZ` | `Europe/Berlin` | Time zone for dates, statistics and backups. |
| `PRINTLOG_PORT` | `8095` | Port on the Docker host. |
| `BACKUP_DIR` | empty (off) | Backup target inside the container, e.g. `/backup` (mount a folder there in `docker-compose.yml`). |
| `BACKUP_HOUR` | `3` | Default hour for the backup schedule. |
| `IMAGE_DELAY_S` | `60` | Seconds to wait before fetching the cover image after a start. |
| `ENERGY_ENTITY` | empty | Only for upgrades from very old versions: energy entity pattern with `{printer}` placeholder. |
| `LOG_LEVEL` | `INFO` | Python log level. |

**Data** is stored in the Docker volume `printlog-data`: `printlog.db` (SQLite) and `images/` (cover images, photos, logo).
**Backups** with `BACKUP_DIR` set: a database copy per run in `db/printlog-YYYY-MM-DD.db` (the last 14 are kept) and a mirror of all images in `images/`.

## Updating

Pull the new version and rebuild: `git pull && docker compose up -d --build` (Portainer: *Pull and redeploy*).
The database is migrated automatically on start. If the blueprint changed, re-import it (or copy the file again)
and reload automations; existing automations keep their inputs.

## The `printlog` event

If you want to feed printlog from something other than the blueprint, fire an event of type `printlog` with these fields:

| Field | Phases | Meaning |
|---|---|---|
| `phase` | all | `start`, `stop` or `final` |
| `printer` | all | Short key of the printer |
| `status` | stop | `finish` or `failed` |
| `energy_kwh` | all | Current meter reading in kWh (empty without plug) |
| `price` | stop, final | Price per kWh (empty = printlog reads `PRICE_ENTITY`) |
| `task_name`, `printer_name`, `printer_start_time`, `progress`, `weight_g` | start, stop | Job details |
| `materials` | start, stop | JSON list `[{slot, name, type, color, grams}]` |
| `image_entity` | start, stop | Image entity of the cover picture |
| `status_entity`, `energy_entity`, `prefix`, `ams`, `ext` | start, stop | Entities printlog stores in its printer settings |

`final` is sent after the cool-down; its meter reading is used for the kWh. Without `final` within 20 minutes,
printlog closes the print with the reading from `stop`.

## Other printers

printlog is built around the Bambu Lab integration (status names, AMS weights per slot, cover image). Electricity
tracking via a smart plug or fixed wattage works for any printer, but for Prusa, Klipper/Moonraker or OctoPrint you
would need your own automation that maps their states to `running` / `finish` / `failed` and fires the event above;
filament weight per slot is usually not available there.

## Troubleshooting

- **`/health` returns 503** – printlog is not connected. Check `HA_URL` (reachable from the container?) and `HA_TOKEN`; see `docker logs printlog`.
- **Prints don't appear** – check the automation's trace in Home Assistant (*Automations → … → Traces*) and that the short name is set.
- **No kWh / "Zählerstand fehlte"** – the plug was offline or the energy sensor is wrong; check *Einstellungen → Drucker*. Set a fixed wattage as a fallback.
- **Prints appear twice** – two automations report the same printer (e.g. an old automation next to the blueprint one). Keep only one per printer.
- **No cover image** – the Bambu integration loads it a moment after the start; printlog fetches it again at the end of the print.

## API

- `GET /health` – 200 when connected to Home Assistant, otherwise 503
- `GET /api/jobs` – prints as JSON (filters as in the UI, e.g. `?printer=x1c&month=2026-11`, plus `limit`, `offset`)
- `GET /api/summary` – totals overall and per printer
- `GET /export.csv` – CSV export with the current filters
- `POST /api/jobs/{id}` – update task_name, customer_id, quantity, job_note
- `POST /api/jobs/{id}/photos` – upload a photo (multipart field `photo`)
- `POST /api/jobs/{id}/delete`, `POST /api/jobs/delete` (`ids=1,2,3`) – move to trash
- `GET /export/{id}/bon-kunde.png`, `bon-intern.png`, `kosten.pdf` – share exports

There is no authentication in printlog itself. Keep it on your local network or put it behind a reverse proxy with access control.
