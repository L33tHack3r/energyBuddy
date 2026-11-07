# Energy Coach

An interactive weekly energy coach for eight fictional Swedish student flats. It turns the supplied synthetic meter, occupancy and market data into per-flat metrics, explainable appliance guesses, and practical **REDUCE** and **SHIFT** suggestions.

The dashboard is backed by a real JSON API and SQLite database. It is not a static mockup. No third-party Python or JavaScript packages are required or installed.

> All readings, occupancy, prices and emissions in this repository are synthetic. Cost is only the spot-energy component. Recommendations and annual projections are illustrative, not guaranteed savings.

## What works

- One normalization and validation service handles CSV imports, single-reading API posts, batch posts, and replayed demo readings.
- SQLite persistence for flats, appliance channels, normalized readings, context, market data and ingestion-quality events.
- `VALID`, `REPAIRED`, `SUSPICIOUS`, and `REJECTED` classifications. Missing or ambiguous values are not silently converted to zero; source reading IDs are deduplicated.
- Per-flat seven-day energy, spot-cost and operational-emissions metrics, joined to hourly occupancy and market data.
- Explainable probabilistic appliance labels with student confirmation/correction.
- Occupancy-based **REDUCE** opportunities grouped by appliance type, with contributing circuit IDs shown. Unknown circuits surface for identification without assumed savings; refrigerators are excluded.
- Price/carbon-based **SHIFT** windows in cheapest, balanced and greenest modes. A shift always reports `0 kWh` change.
- A weekly summary and email preview generated from the same API response, plus clearly labeled annualized opportunity projections.
- A replay button that submits a new reading through the same ingestion function and updates the database-backed dashboard.

The inference is a deterministic, explainable heuristic, not a trained model or LLM. Arithmetic, cleaning and ranking stay in code. The market, meter-replay and notification integrations have provider seams so synthetic implementations can later be replaced.

## Architecture

```text
CSV dataset ─┐
             ├─> shared cleaner/validator ─> SQLite ─> analytics and recommendations
POST /api ───┤                                           │
             └─ demo replay provider ───────────────────┘
                                                         │
                                            JSON API <──> dashboard
```

The backend uses only Python's standard library (`http.server`, `sqlite3`, and `csv`); the frontend is plain HTML, CSS and JavaScript. Docker Compose is available to run the application in a container without adding project dependencies to the host. SQLite lives in a Docker-managed named volume when using Compose.

## Run it

From the project directory, choose either a containerized run or a local run.

### Isolated container (recommended)

Requires Docker Desktop (or Docker Engine) with the Compose plugin. This keeps the app runtime inside a container and does not install project packages on your computer.

```sh
cd /path/to/lnuHackathon
docker compose up --build
```

Wait for the log line saying the API is listening, then open [http://localhost:8000](http://localhost:8000). On first start, the app imports the included CSV data into SQLite; this can take a little while. The database persists in the `energy-coach-state` volume.

Stop the app with **Ctrl+C**. To stop and remove the container while preserving the database, run `docker compose down`. To also delete the saved demo database, run `docker compose down --volumes`.

### Without Docker

Requires Python 3.9 or newer. No packages need to be installed:

```sh
cd /path/to/lnuHackathon
python3 backend/server.py
```

Leave that terminal running and open [http://localhost:8000](http://localhost:8000) in a browser. On first start, the app imports the included CSV data into SQLite; this can take a little while. The local database is created at `data/energy_coach.sqlite3`; the source CSVs remain unchanged. Stop the server with **Ctrl+C**.

To use a different local database or data directory:

```sh
cd /path/to/lnuHackathon
DATABASE_PATH=/tmp/energy-coach.sqlite3 DATA_DIR="$(pwd)" python3 backend/server.py
```

The app listens on port `8000`; if that port is in use, set `PORT` to another port, for example `PORT=8080 python3 backend/server.py`, then open `http://localhost:8080`.

## Demo flow

1. Select one of the eight flats in the left sidebar.
2. Review the seven-day energy, spot-cost and CO₂e metrics and occupancy-aware chart.
3. Use **Cheapest**, **Balanced** and **Greenest** to recompute the next-day window and recommendations.
4. Open **Circuits** to inspect probabilistic guesses and confirm a label. A confirmed label takes precedence in subsequent advice.
5. Open **Data quality** to see the raw/valid/repaired/suspicious/rejected counts.
6. Select **Inject a reading**. The app sends `330 Wh` through ingestion, normalizes it to `0.33 kWh`, persists it and reloads the analytics.
7. Open **Daily nudge**, choose cheapest/balanced/greenest, and use **Simulate today's push**. The browser requests the daily note from the API and posts the chosen note to the delivery API. The simulated delivery is stored in SQLite and is limited to one per flat per UTC day.
8. Open **Weekly note** for the generated summary, email preview and explicitly labeled annual opportunity projection.

The market view uses supplied hourly rows when available. For missing hours, the synthetic provider projects from historical weekday/local-hour averages and marks those rows as projected. The supplied market data is historical: when its next modeled date is already in the past, the dashboard clearly labels it a historical scenario rather than tomorrow's forecast. All displayed hours and dates are Sweden-local; UTC timestamps remain available in API results.

## API

All responses are JSON except the dashboard assets. Start the server before calling the API.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Health check |
| `GET` | `/api/flats` | Flat list and weekly headline metrics |
| `GET` | `/api/flats/{flat_id}` | Flat metadata |
| `GET` | `/api/flats/{flat_id}/usage?days=7` | Consumption series, occupancy and cost/emissions metrics |
| `GET` | `/api/flats/{flat_id}/appliances` | Inferred/confirmed circuit labels and features |
| `PATCH` | `/api/flats/{flat_id}/appliances/{appliance_id}` | Confirm or clear `confirmed_type` |
| `GET` | `/api/flats/{flat_id}/recommendations?mode=balanced` | Ranked REDUCE and SHIFT advice |
| `POST` | `/api/flats/{flat_id}/recommendations/generate` | Recalculate recommendations |
| `GET` | `/api/flats/{flat_id}/weekly-summary` | Summary facts and matching email HTML preview |
| `GET` | `/api/flats/{flat_id}/daily-note?mode=balanced` | Generate a timing note from recent recurring device usage and the next modeled market day |
| `POST` | `/api/flats/{flat_id}/daily-notification` | Simulate delivery of today's nudge, persisted once per flat per UTC day |
| `GET` | `/api/market/tomorrow?flat_id=F001&mode=cheapest` | Hourly price/carbon data and suggested window |
| `GET` | `/api/data-quality` | Ingestion classification counts and recent events |
| `POST` | `/api/readings` | Ingest one reading |
| `POST` | `/api/readings/batch` | Ingest up to 500 readings |
| `POST` | `/api/demo/replay` | Ingest one simulated new reading |

Example:

```sh
curl -X POST http://localhost:8000/api/readings \
  -H 'Content-Type: application/json' \
  -d '{
    "reading_id": "r1234",
    "flat_id": "F003",
    "appliance_id": "F003-A04",
    "timestamp_utc": "2026-10-29T14:00:00Z",
    "energy_value": "330",
    "energy_unit": "Wh"
  }'
```

The response includes `status`, normalized kWh, `data_quality` and repair notes. Unrecoverable requests are returned as rejected and recorded in the quality-event table; duplicate source IDs do not create a second energy reading.

The older flat-unscoped `PATCH /api/appliances/{appliance_id}` route is also accepted when the circuit ID is unique (or the request includes `flat_id`).

The dashboard calls these daily-note endpoints directly; it does not calculate the recommendation in browser code. For example:

```sh
curl 'http://localhost:8000/api/flats/F001/daily-note?mode=greenest'

curl -X POST http://localhost:8000/api/flats/F001/daily-notification \
  -H 'Content-Type: application/json' \
  -d '{"mode":"balanced"}'
```

The preview endpoint derives device-specific advice from trusted, recent recurring dishwasher/washing-machine runs and the hourly market data. It returns usual and suggested Sweden-local start hours, corresponding UTC hours, estimated SEK/CO₂e differences, run counts, and zero kWh change for shifting. A suggested time must be within six hours of the usual start, between 06:00 and 23:00 local time, and improve the selected mode by at least 0.05 on its normalized score, filtering out marginal timing noise. Each note also gives the cheapest, cleanest, and balanced market hour; the 24-hour chart uses the same API values. The send endpoint uses the `NotificationProvider` simulation and persists delivery status; it is a backend API action, not an operating-system push or a scheduled production service. The provided CSVs are synthetic hackathon data; values and recommendations are calculated dynamically from the database, not hard-coded dashboard fixtures. Historical projections are not live market forecasts.

## Calculation notes and limitations

- Readings represent energy in the one-hour interval, not instantaneous power or a cumulative register.
- Spot cost is `kWh × spot_sek_per_kwh`; it excludes retail markup, network fees, tax, VAT and fixed charges. Negative market prices are preserved.
- Operational emissions are `kWh × carbon_gco2e_per_kwh / 1000`, using the supplied synthetic factors.
- **REDUCE** groups matching inferred/confirmed appliance types and lists their circuit IDs. Away-time advice excludes refrigerators. Portable heaters and other identified loads can be considered; an unknown circuit is surfaced for identification only, with no assumed avoidable energy or savings. For identified types, the estimate assumes half of observed use is avoidable.
- **SHIFT** detects short contiguous high-energy runs repeated across distinct days, averages their local start hour circularly, then compares it with a mode-selected hour within the feasible window. Suspicious readings are excluded. It changes estimated price/emissions, never kWh. Scheduling safety and actual flexibility are not known.
- Balanced mode equally weights normalized price and carbon scores. Cheapest and greenest optimize only the respective supplied metric.
- Appliance labels are probabilistic usage-pattern heuristics. Hourly data cannot reliably identify every appliance, so labels should be checked and corrected by the student.
- Annual projections extrapolate the current seven-day opportunity by `365 / observed days`. One synthetic autumn week is not evidence of annual savings; seasonality, rebound and behavior changes are not modeled.
- Actual email/push delivery, a live meter, live market data, authentication, an automatic scheduler and production deployment are not implemented. Daily notification delivery is simulated through the backend API; content is computed from stored usage and the market provider.

## MVP and stretch scope

**Essential functional MVP:** SQL persistence, CSV import through the ingestion layer, validation/cleaning, REST ingestion, per-flat analytics, REDUCE/SHIFT calculations, recommendations, a connected dashboard and generated weekly summaries.

**High-value demo enhancements:** appliance inference and correction, three optimization modes, projected next-day market windows, quality dashboard, real ingestion replay, shared email preview and API-backed daily notification simulation.

**Optional stretch goals:** real email delivery, live utility/market providers, authentication, deployment and trained appliance classification.

## Tests

Tests use only the Python standard library and create a temporary SQLite database:

```sh
python3 -m unittest discover -s tests -v
```
