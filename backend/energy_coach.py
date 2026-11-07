import csv
import json
import math
import os
import re
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT))
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", ROOT / "data" / "energy_coach.sqlite3"))
QUALITY_STATUSES = ("valid", "repaired", "suspicious", "rejected")
APPLIANCE_TYPES = (
    "Dishwasher",
    "Washing machine",
    "Portable heater",
    "Refrigerator",
    "Cooking appliance",
    "Lighting",
    "Electronics",
    "Unknown appliance",
)
AWAY_REDUCE_TYPES = {
    appliance_type.lower()
    for appliance_type in APPLIANCE_TYPES
    if appliance_type.lower() != "refrigerator"
}
LOCAL_TIMEZONE = ZoneInfo("Europe/Stockholm")


def _as_local(timestamp):
    return datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    ).astimezone(LOCAL_TIMEZONE)


def _recurring_appliance_runs(readings):
    runs = []
    for reading in readings:
        if reading["energy_kwh"] < 0.20:
            continue
        stamp = datetime.strptime(reading["timestamp_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        if not runs or stamp - runs[-1]["last"] != timedelta(hours=1):
            local_start = stamp.astimezone(LOCAL_TIMEZONE)
            runs.append({
                "first": stamp,
                "last": stamp,
                "local_date": local_start.date(),
                "start_hour": local_start.hour,
                "energy_kwh": reading["energy_kwh"],
            })
        else:
            runs[-1]["last"] = stamp
            runs[-1]["energy_kwh"] += reading["energy_kwh"]
    runs = [
        run for run in runs
        if 0.25 <= run["energy_kwh"] <= 4
        and (run["last"] - run["first"]).total_seconds() / 3600 < 5
    ]
    return runs if len({run["local_date"] for run in runs}) >= 2 else []


def _round_nonzero(value, digits):
    rounded = round(value, digits)
    return rounded if rounded else 0


@contextmanager
def connect():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def canonical_timestamp(value):
    text = str(value or "").strip()
    if not text:
        raise ValueError("timestamp is missing")
    if re.fullmatch(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}", text):
        parsed = datetime.strptime(text, "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc)
    else:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_reading(payload, known_flats=None):
    repairs = []
    raw_reading_id = str(payload.get("reading_id") or payload.get("source_reading_id") or "").strip()
    reading_id = raw_reading_id.upper()
    flat_id = str(payload.get("flat_id") or "").strip().upper()
    appliance_id = str(payload.get("appliance_id") or "").strip().upper()
    raw_value = str(payload.get("energy_value") or "").strip()
    raw_unit = str(payload.get("energy_unit") or "").strip()
    suspicious = []

    if reading_id != raw_reading_id:
        repairs.append("normalized reading_id casing")
    if not reading_id:
        return {"status": "rejected", "repairs": repairs, "reason": "reading_id is required"}
    if not flat_id or not re.fullmatch(r"F\d{3}", flat_id):
        return {"status": "rejected", "repairs": repairs, "reason": "flat_id is malformed"}
    if known_flats is not None and flat_id not in known_flats:
        return {"status": "rejected", "repairs": repairs, "reason": "flat_id is unknown"}
    if not appliance_id or not re.fullmatch(r"[A-Z0-9-]{2,32}", appliance_id):
        return {"status": "rejected", "repairs": repairs, "reason": "appliance_id is malformed"}
    if flat_id != str(payload.get("flat_id") or "").strip():
        repairs.append("normalized flat_id casing or whitespace")
    if appliance_id != str(payload.get("appliance_id") or "").strip():
        repairs.append("normalized appliance_id casing or whitespace")
    if not re.fullmatch(r"[A-Z0-9-]{2,32}", appliance_id):
        return {"status": "rejected", "repairs": repairs, "reason": "appliance_id is malformed"}
    if not re.fullmatch(r"[A-Z0-9-]+", reading_id):
        return {"status": "rejected", "repairs": repairs, "reason": "reading_id is malformed"}

    try:
        timestamp = canonical_timestamp(payload.get("timestamp_utc"))
    except (ValueError, TypeError, OverflowError):
        return {"status": "rejected", "repairs": repairs, "reason": "timestamp is invalid"}
    if timestamp != str(payload.get("timestamp_utc") or "").strip():
        repairs.append("normalized timestamp to UTC")

    if not raw_value:
        return {"status": "rejected", "repairs": repairs, "reason": "energy_value is missing"}
    value_text = raw_value
    if "," in value_text and "." not in value_text and re.fullmatch(r"[+-]?\d+,\d+", value_text):
        value_text = value_text.replace(",", ".")
        repairs.append("converted decimal comma")
    try:
        energy_value = float(value_text)
    except (ValueError, TypeError):
        return {"status": "rejected", "repairs": repairs, "reason": "energy_value is not numeric"}
    if not math.isfinite(energy_value):
        return {"status": "rejected", "repairs": repairs, "reason": "energy_value is not finite"}

    unit = raw_unit.lower()
    unit = unit.replace(" ", "")
    if unit in ("kwh", "kw·h"):
        normalized_unit = "kWh"
    elif unit in ("wh", "w·h"):
        normalized_unit = "Wh"
    else:
        return {"status": "rejected", "repairs": repairs, "reason": "energy_unit is unsupported"}
    if normalized_unit != raw_unit:
        repairs.append("normalized energy unit")
    energy_kwh = energy_value / 1000 if normalized_unit == "Wh" else energy_value
    if normalized_unit == "Wh":
        repairs.append("converted Wh to kWh")

    if not appliance_id.startswith(flat_id + "-"):
        suspicious.append("appliance_id does not match flat_id")
    if energy_kwh < 0:
        suspicious.append("negative energy value")
    if energy_kwh > 5:
        suspicious.append("hourly energy exceeds 5 kWh")
    if suspicious:
        repairs.extend(suspicious)
        status = "suspicious"
    elif repairs:
        status = "repaired"
    else:
        status = "valid"
    return {
        "status": status,
        "repairs": repairs,
        "reading": {
            "reading_id": reading_id,
            "flat_id": flat_id,
            "appliance_id": appliance_id,
            "timestamp_utc": timestamp,
            "energy_kwh": energy_kwh,
            "original_value": raw_value,
            "original_unit": raw_unit,
        },
    }


def init_db(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS flats (
            flat_id TEXT PRIMARY KEY,
            residents INTEGER NOT NULL,
            floor_area_m2 REAL NOT NULL,
            price_area TEXT NOT NULL,
            main_heating TEXT NOT NULL,
            tariff_id TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS appliances (
            appliance_id TEXT NOT NULL,
            flat_id TEXT NOT NULL REFERENCES flats(flat_id),
            inferred_type TEXT NOT NULL DEFAULT 'Unknown appliance',
            inference_confidence REAL NOT NULL DEFAULT 0,
            inference_candidates_json TEXT NOT NULL DEFAULT '[]',
            confirmed_type TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (flat_id, appliance_id)
        );
        CREATE TABLE IF NOT EXISTS energy_readings (
            id INTEGER PRIMARY KEY,
            source_reading_id TEXT NOT NULL UNIQUE,
            flat_id TEXT NOT NULL REFERENCES flats(flat_id),
            appliance_id TEXT NOT NULL,
            timestamp_utc TEXT NOT NULL,
            energy_kwh REAL NOT NULL,
            original_value TEXT,
            original_unit TEXT,
            data_quality_status TEXT NOT NULL,
            repair_notes TEXT NOT NULL,
            ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (flat_id, appliance_id) REFERENCES appliances(flat_id, appliance_id)
        );
        CREATE INDEX IF NOT EXISTS idx_readings_flat_time ON energy_readings(flat_id, timestamp_utc);
        CREATE INDEX IF NOT EXISTS idx_readings_appliance_time ON energy_readings(appliance_id, timestamp_utc);
        CREATE TABLE IF NOT EXISTS context_readings (
            flat_id TEXT NOT NULL REFERENCES flats(flat_id),
            timestamp_utc TEXT NOT NULL,
            indoor_temp_c REAL,
            outdoor_temp_c REAL,
            occupants_home INTEGER,
            PRIMARY KEY (flat_id, timestamp_utc)
        );
        CREATE TABLE IF NOT EXISTS hourly_market_data (
            timestamp_utc TEXT NOT NULL,
            price_area TEXT NOT NULL,
            spot_sek_per_kwh REAL NOT NULL,
            carbon_gco2e_per_kwh REAL NOT NULL,
            is_projected INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (timestamp_utc, price_area)
        );
        CREATE TABLE IF NOT EXISTS ingestion_events (
            id INTEGER PRIMARY KEY,
            source_reading_id TEXT,
            status TEXT NOT NULL,
            repairs_json TEXT NOT NULL,
            reason TEXT,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS import_issues (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            source_timestamp TEXT,
            reason TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS daily_notifications (
            id INTEGER PRIMARY KEY,
            flat_id TEXT NOT NULL REFERENCES flats(flat_id),
            delivery_date_utc TEXT NOT NULL,
            mode TEXT NOT NULL,
            notification_json TEXT NOT NULL,
            channel TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (flat_id, delivery_date_utc)
        );
        CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    connection.commit()


def _insert_event(connection, payload, status, repairs, reason=None):
    connection.execute(
        """INSERT INTO ingestion_events
           (source_reading_id, status, repairs_json, reason, raw_json)
           VALUES (?, ?, ?, ?, ?)""",
        (
            str(payload.get("reading_id") or payload.get("source_reading_id") or "").strip() or None,
            status,
            json.dumps(repairs),
            reason,
            json.dumps(payload, ensure_ascii=False, default=str),
        ),
    )


def ingest_reading(connection, payload, known_flats=None):
    if not isinstance(payload, dict):
        payload = {"_invalid_payload": payload}
    result = clean_reading(payload, known_flats)
    if result["status"] == "rejected":
        _insert_event(connection, payload, "rejected", result["repairs"], result["reason"])
        return {"status": "rejected", "data_quality": "rejected", "repairs": result["repairs"], "reason": result["reason"]}

    reading = result["reading"]
    duplicate = connection.execute(
        "SELECT 1 FROM energy_readings WHERE source_reading_id = ?",
        (reading["reading_id"],),
    ).fetchone()
    if duplicate:
        reason = "reading_id has already been ingested"
        _insert_event(connection, payload, "rejected", result["repairs"], reason)
        return {"status": "rejected", "data_quality": "rejected", "repairs": result["repairs"], "reason": reason}

    flat = connection.execute("SELECT price_area FROM flats WHERE flat_id = ?", (reading["flat_id"],)).fetchone()
    if flat is None:
        reason = "flat_id is unknown"
        _insert_event(connection, payload, "rejected", result["repairs"], reason)
        return {"status": "rejected", "data_quality": "rejected", "repairs": result["repairs"], "reason": reason}
    connection.execute(
        """INSERT OR IGNORE INTO appliances (appliance_id, flat_id)
           VALUES (?, ?)""",
        (reading["appliance_id"], reading["flat_id"]),
    )
    connection.execute(
        """INSERT INTO energy_readings
           (source_reading_id, flat_id, appliance_id, timestamp_utc, energy_kwh,
            original_value, original_unit, data_quality_status, repair_notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            reading["reading_id"],
            reading["flat_id"],
            reading["appliance_id"],
            reading["timestamp_utc"],
            reading["energy_kwh"],
            reading["original_value"],
            reading["original_unit"],
            result["status"],
            json.dumps(result["repairs"]),
        ),
    )
    _insert_event(connection, payload, result["status"], result["repairs"])
    return {
        "status": "accepted",
        "data_quality": result["status"],
        "repairs": result["repairs"],
        "normalized_energy_kwh": round(reading["energy_kwh"], 6),
        "reading_id": reading["reading_id"],
    }


def _read_csv(filename):
    with (DATA_DIR / filename).open("r", encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def seed_dataset(connection):
    seeded = connection.execute("SELECT value FROM app_state WHERE key = 'dataset_seeded'").fetchone()
    if seeded:
        return
    for flat in _read_csv("flats.csv"):
        connection.execute(
            """INSERT OR REPLACE INTO flats
               (flat_id, residents, floor_area_m2, price_area, main_heating, tariff_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                flat["flat_id"].strip(),
                int(flat["residents"]),
                float(flat["floor_area_m2"]),
                flat["price_area"].strip(),
                flat["main_heating"].strip(),
                flat["tariff_id"].strip(),
            ),
        )
    known_flats = {row["flat_id"] for row in connection.execute("SELECT flat_id FROM flats")}
    for market in _read_csv("hourly_prices.csv"):
        try:
            price_text = (market.get("spot_sek_per_kwh") or "").strip()
            carbon_text = (market.get("carbon_gco2e_per_kwh") or "").strip()
            if not price_text or not carbon_text:
                raise ValueError("missing price or carbon intensity; row excluded, not imputed")
            price = float(price_text)
            carbon = float(carbon_text)
            if not math.isfinite(price) or not math.isfinite(carbon):
                raise ValueError("non-finite price or carbon intensity; row excluded")
            connection.execute(
                """INSERT OR REPLACE INTO hourly_market_data
                   (timestamp_utc, price_area, spot_sek_per_kwh, carbon_gco2e_per_kwh)
                   VALUES (?, ?, ?, ?)""",
                (canonical_timestamp(market["timestamp_utc"]), market["price_area"].strip(), price, carbon),
            )
        except (ValueError, TypeError, KeyError) as error:
            connection.execute(
                """INSERT INTO import_issues (source, source_timestamp, reason, raw_json)
                   VALUES ('hourly_prices.csv', ?, ?, ?)""",
                (market.get("timestamp_utc"), str(error), json.dumps(market)),
            )
    for context in _read_csv("hourly_context.csv"):
        try:
            connection.execute(
                """INSERT OR REPLACE INTO context_readings
                   (flat_id, timestamp_utc, indoor_temp_c, outdoor_temp_c, occupants_home)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    context["flat_id"].strip(),
                    canonical_timestamp(context["timestamp_utc"]),
                    float(context["indoor_temp_c"]) if context["indoor_temp_c"].strip() else None,
                    float(context["outdoor_temp_c"]) if context["outdoor_temp_c"].strip() else None,
                    int(context["occupants_home"]) if context["occupants_home"].strip() else None,
                ),
            )
        except (ValueError, TypeError):
            connection.execute(
                """INSERT INTO import_issues (source, source_timestamp, reason, raw_json)
                   VALUES ('hourly_context.csv', ?, 'invalid context row; excluded', ?)""",
                (context.get("timestamp_utc"), json.dumps(context)),
            )
    for source in _read_csv("energy_readings.csv"):
        ingest_reading(connection, source, known_flats)
    connection.execute("INSERT INTO app_state (key, value) VALUES ('dataset_seeded', 'true')")
    connection.commit()
    infer_all_appliances(connection)


def _latest_timestamp(connection, table):
    row = connection.execute(f"SELECT MAX(timestamp_utc) AS latest FROM {table}").fetchone()
    return row["latest"] if row and row["latest"] else None


def _projected_market(connection, timestamp, price_area):
    requested_local = _as_local(timestamp)
    ordered = connection.execute(
        """SELECT timestamp_utc, spot_sek_per_kwh, carbon_gco2e_per_kwh
           FROM hourly_market_data WHERE price_area = ? AND is_projected = 0
           ORDER BY timestamp_utc""",
        (price_area,),
    ).fetchall()
    matching = [
        row for row in ordered
        if _as_local(row["timestamp_utc"]).hour == requested_local.hour
        and _as_local(row["timestamp_utc"]).weekday() == requested_local.weekday()
    ]
    if len(matching) < 3:
        matching = [
            row for row in ordered
            if _as_local(row["timestamp_utc"]).hour == requested_local.hour
        ]
    base = matching or ordered
    price = sum(row["spot_sek_per_kwh"] for row in base) / len(base)
    carbon = sum(row["carbon_gco2e_per_kwh"] for row in base) / len(base)
    connection.execute(
        """INSERT OR IGNORE INTO hourly_market_data
           (timestamp_utc, price_area, spot_sek_per_kwh, carbon_gco2e_per_kwh, is_projected)
           VALUES (?, ?, ?, ?, 1)""",
        (timestamp, price_area, price, carbon),
    )
    return {"timestamp_utc": timestamp, "spot_sek_per_kwh": price, "carbon_gco2e_per_kwh": carbon, "projected": True}


class MarketDataProvider:
    def tomorrow(self, connection, flat_id, mode):
        raise NotImplementedError


class SyntheticMarketDataProvider(MarketDataProvider):
    def tomorrow(self, connection, flat_id, mode):
        return _synthetic_market_data(connection, flat_id, mode)


MARKET_DATA_PROVIDER = SyntheticMarketDataProvider()


def market_data(connection, flat_id, mode="balanced"):
    return MARKET_DATA_PROVIDER.tomorrow(connection, flat_id, mode)


def _synthetic_market_data(connection, flat_id, mode="balanced"):
    mode = mode.lower()
    if mode not in ("cheapest", "balanced", "greenest"):
        raise ValueError("mode must be cheapest, balanced, or greenest")
    flat = connection.execute("SELECT price_area FROM flats WHERE flat_id = ?", (flat_id,)).fetchone()
    if not flat:
        raise KeyError("flat not found")
    latest_row = connection.execute(
        "SELECT MAX(timestamp_utc) AS latest FROM hourly_market_data WHERE is_projected = 0"
    ).fetchone()
    latest = latest_row["latest"] if latest_row else None
    if not latest:
        return {"flat_id": flat_id, "mode": mode, "hours": [], "window": None}
    latest_local = _as_local(latest)
    forecast_date = latest_local.date() + timedelta(days=1)
    hours = []
    for offset in range(24):
        local_start = datetime.combine(forecast_date, time(offset), tzinfo=LOCAL_TIMEZONE)
        stamp = local_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = connection.execute(
            """SELECT spot_sek_per_kwh, carbon_gco2e_per_kwh, is_projected
               FROM hourly_market_data WHERE timestamp_utc = ? AND price_area = ?""",
            (stamp, flat["price_area"]),
        ).fetchone()
        if row:
            hours.append(
                {
                    "timestamp_utc": stamp,
                    "timestamp_local": local_start.isoformat(),
                    "hour": offset,
                    "hour_local": offset,
                    "hour_utc": local_start.astimezone(timezone.utc).hour,
                    "spot_sek_per_kwh": row["spot_sek_per_kwh"],
                    "carbon_gco2e_per_kwh": row["carbon_gco2e_per_kwh"],
                    "projected": bool(row["is_projected"]),
                }
            )
        else:
            projected = _projected_market(connection, stamp, flat["price_area"])
            hours.append(
                {
                    "hour": offset,
                    "hour_local": offset,
                    "hour_utc": local_start.astimezone(timezone.utc).hour,
                    "timestamp_local": local_start.isoformat(),
                    **projected,
                }
            )
    prices = [item["spot_sek_per_kwh"] for item in hours]
    carbons = [item["carbon_gco2e_per_kwh"] for item in hours]
    price_range = max(prices) - min(prices) or 1
    carbon_range = max(carbons) - min(carbons) or 1
    for item in hours:
        price_score = (item["spot_sek_per_kwh"] - min(prices)) / price_range
        carbon_score = (item["carbon_gco2e_per_kwh"] - min(carbons)) / carbon_range
        item["optimization_score"] = {
            "cheapest": price_score,
            "greenest": carbon_score,
            "balanced": (price_score + carbon_score) / 2,
        }[mode]
    window = min(hours, key=lambda item: item["optimization_score"])
    today_local = datetime.now(LOCAL_TIMEZONE).date()
    observed_forecast_hours = sum(not item["projected"] for item in hours)
    return {
        "flat_id": flat_id,
        "mode": mode,
        "date_local": forecast_date.isoformat(),
        "date_utc": datetime.strptime(hours[0]["timestamp_utc"], "%Y-%m-%dT%H:%M:%SZ").date().isoformat(),
        "as_of_utc": latest,
        "forecast_kind": "observed" if observed_forecast_hours == 24 else "mixed" if observed_forecast_hours else "historical_projection",
        "observed_forecast_hours": observed_forecast_hours,
        "is_historical_scenario": forecast_date < today_local,
        "hours": hours,
        "window": window,
    }


def _reading_rows(connection, flat_id):
    return connection.execute(
        """SELECT r.*, c.occupants_home, c.indoor_temp_c, c.outdoor_temp_c,
                  m.spot_sek_per_kwh, m.carbon_gco2e_per_kwh
           FROM energy_readings r
           LEFT JOIN context_readings c
             ON c.flat_id = r.flat_id AND c.timestamp_utc = r.timestamp_utc
           LEFT JOIN flats f ON f.flat_id = r.flat_id
           LEFT JOIN hourly_market_data m
             ON m.timestamp_utc = r.timestamp_utc AND m.price_area = f.price_area
           WHERE r.flat_id = ? AND r.data_quality_status IN ('valid', 'repaired')
           ORDER BY r.timestamp_utc""",
        (flat_id,),
    ).fetchall()


def flat_usage(connection, flat_id, days=7):
    flat = connection.execute("SELECT * FROM flats WHERE flat_id = ?", (flat_id,)).fetchone()
    if not flat:
        raise KeyError("flat not found")
    rows = _reading_rows(connection, flat_id)
    latest = max((row["timestamp_utc"] for row in rows), default=None)
    if latest is None:
        return {"flat_id": flat_id, "metrics": {}, "series": []}
    latest_dt = datetime.strptime(latest, "%Y-%m-%dT%H:%M:%SZ")
    start = latest_dt - timedelta(hours=max(1, days) * 24 - 1)
    previous_start = start - timedelta(days=max(1, days))
    recent = []
    previous = []
    for row in rows:
        stamp = datetime.strptime(row["timestamp_utc"], "%Y-%m-%dT%H:%M:%SZ")
        if stamp >= start:
            recent.append(row)
        elif stamp >= previous_start:
            previous.append(row)
    hourly = defaultdict(lambda: {"energy_kwh": 0.0, "cost_sek": 0.0, "co2e_kg": 0.0, "occupants_home": None})
    for row in recent:
        point = hourly[row["timestamp_utc"]]
        point["energy_kwh"] += row["energy_kwh"]
        if row["spot_sek_per_kwh"] is not None:
            point["cost_sek"] += row["energy_kwh"] * row["spot_sek_per_kwh"]
        if row["carbon_gco2e_per_kwh"] is not None:
            point["co2e_kg"] += row["energy_kwh"] * row["carbon_gco2e_per_kwh"] / 1000
        point["occupants_home"] = row["occupants_home"]

    def totals(items):
        cost_coverage = sum(1 for row in items if row["spot_sek_per_kwh"] is not None)
        carbon_coverage = sum(1 for row in items if row["carbon_gco2e_per_kwh"] is not None)
        return {
            "energy_kwh": sum(row["energy_kwh"] for row in items),
            "spot_cost_sek": sum(
                row["energy_kwh"] * row["spot_sek_per_kwh"]
                for row in items if row["spot_sek_per_kwh"] is not None
            ),
            "co2e_kg": sum(
                row["energy_kwh"] * row["carbon_gco2e_per_kwh"] / 1000
                for row in items if row["carbon_gco2e_per_kwh"] is not None
            ),
            "market_coverage_percent": round(100 * cost_coverage / len(items), 1) if items else 0,
            "carbon_coverage_percent": round(100 * carbon_coverage / len(items), 1) if items else 0,
        }

    current = totals(recent)
    prior = totals(previous)
    current["previous_week_kwh"] = round(prior["energy_kwh"], 3)
    current["week_change_percent"] = (
        round(100 * (current["energy_kwh"] - prior["energy_kwh"]) / prior["energy_kwh"], 1)
        if prior["energy_kwh"] else None
    )
    current["occupancy_coverage_percent"] = round(
        100 * sum(1 for row in recent if row["occupants_home"] is not None) / len(recent), 1
    ) if recent else 0
    current["unoccupied_kwh"] = sum(
        row["energy_kwh"] for row in recent if row["occupants_home"] == 0
    )
    current["period_start_utc"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    current["period_end_utc"] = latest
    series = [
        {
            "timestamp_utc": stamp,
            **{key: round(value, 4) if isinstance(value, float) else value for key, value in point.items()},
        }
        for stamp, point in sorted(hourly.items())
    ]
    return {"flat_id": flat_id, "flat": dict(flat), "metrics": current, "series": series}


def infer_all_appliances(connection):
    appliances = connection.execute("SELECT appliance_id, flat_id FROM appliances").fetchall()
    for appliance in appliances:
        rows = connection.execute(
            """SELECT r.energy_kwh, r.timestamp_utc, c.occupants_home, c.outdoor_temp_c
               FROM energy_readings r LEFT JOIN context_readings c
                 ON c.flat_id = r.flat_id AND c.timestamp_utc = r.timestamp_utc
               WHERE r.appliance_id = ? AND r.flat_id = ?
                 AND r.data_quality_status IN ('valid', 'repaired')
               ORDER BY r.timestamp_utc""",
            (appliance["appliance_id"], appliance["flat_id"]),
        ).fetchall()
        if not rows:
            continue
        values = [row["energy_kwh"] for row in rows]
        active = [row for row in rows if row["energy_kwh"] > 0.015]
        event_rows = [row for row in rows if row["energy_kwh"] >= 0.20]
        events = []
        for row in event_rows:
            stamp = datetime.strptime(row["timestamp_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if not events or stamp - events[-1]["last"] != timedelta(hours=1):
                events.append({"first": stamp, "last": stamp, "energy": row["energy_kwh"]})
            else:
                events[-1]["last"] = stamp
                events[-1]["energy"] += row["energy_kwh"]

        event_days = {event["first"].astimezone(LOCAL_TIMEZONE).date() for event in events}
        event_hours = [event["first"].astimezone(LOCAL_TIMEZONE).hour for event in events]
        evening_share = (
            sum(1 for hour in event_hours if 17 <= hour <= 22) / len(event_hours)
            if event_hours else 0
        )
        mean_event_kwh = sum(event["energy"] for event in events) / len(events) if events else 0
        mean_event_hours = (
            sum((event["last"] - event["first"]).total_seconds() / 3600 + 1 for event in events) / len(events)
            if events else 0
        )
        event_recurrence = min(1, len(event_days) / 8)
        cycle_energy_fit = 1 if 0.25 <= mean_event_kwh <= 3 else 0.2
        cycle_duration_fit = 1 if 1 <= mean_event_hours <= 4 else 0.4
        cycle_evidence = event_recurrence * cycle_energy_fit * cycle_duration_fit
        active_fraction = len(active) / len(rows)
        active_mean = sum(row["energy_kwh"] for row in active) / len(active) if active else 0
        home_share = (
            sum(1 for row in active if row["occupants_home"] and row["occupants_home"] > 0) / len(active)
            if active else 0
        )
        local_hours = [_as_local(row["timestamp_utc"]).hour for row in active]
        evening_active_share = (
            sum(1 for hour in local_hours if 17 <= hour <= 23) / len(local_hours)
            if local_hours else 0
        )
        hourly_variation = (
            (max(values) - min(values)) / max(0.01, abs(active_mean))
            if values else 0
        )

        scores = {
            "Dishwasher": 0.03 + cycle_evidence * (0.42 + 0.38 * evening_share),
            "Washing machine": 0.03 + cycle_evidence * (0.48 - 0.20 * evening_share),
            "Portable heater": (
                0.03 + min(0.88, active_fraction * 0.72)
                if active_mean >= 0.12 and active_fraction >= 0.12 else 0.04
            ),
            "Refrigerator": (
                0.08 + min(0.72, active_fraction * 0.75)
                if active_fraction >= 0.55 and max(values) <= 0.30 else 0.04
            ),
            "Cooking appliance": (
                0.04 + min(0.55, home_share * 0.5 + cycle_evidence * 0.25)
                if home_share > 0.5 else 0.04
            ),
            "Lighting": (
                0.04 + min(0.5, evening_active_share * 0.45)
                if active_mean < 0.20 and evening_active_share > 0.15 else 0.04
            ),
            "Electronics": (
                0.08 + min(0.45, active_fraction * 0.35)
                if active_mean < 0.20 else 0.04
            ),
            "Unknown appliance": 0.16 + min(0.25, hourly_variation * 0.03),
        }
        top = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:3]
        total = sum(score for _, score in top)
        ranked = [(name, score / total) for name, score in top]
        connection.execute(
            """UPDATE appliances SET inferred_type = ?, inference_confidence = ?,
               inference_candidates_json = ?, updated_at = CURRENT_TIMESTAMP
               WHERE appliance_id = ? AND flat_id = ?""",
            (
                ranked[0][0],
                ranked[0][1],
                json.dumps([{"type": name, "probability": probability} for name, probability in ranked[:3]]),
                appliance["appliance_id"],
                appliance["flat_id"],
            ),
        )
    connection.commit()


def list_appliances(connection, flat_id):
    latest = _latest_timestamp(connection, "energy_readings")
    start = (
        datetime.strptime(latest, "%Y-%m-%dT%H:%M:%SZ") - timedelta(hours=167)
    ).strftime("%Y-%m-%dT%H:%M:%SZ") if latest else ""
    rows = connection.execute(
        """SELECT DISTINCT a.*
           FROM appliances a JOIN energy_readings r
             ON r.appliance_id = a.appliance_id AND r.flat_id = a.flat_id
           WHERE a.flat_id = ? AND r.data_quality_status IN ('valid', 'repaired')
           ORDER BY a.appliance_id""",
        (flat_id,),
    ).fetchall()
    result = []
    for row in rows:
        active = connection.execute(
            """SELECT r.energy_kwh, r.timestamp_utc, c.occupants_home
               FROM energy_readings r LEFT JOIN context_readings c
                 ON c.flat_id = r.flat_id AND c.timestamp_utc = r.timestamp_utc
               WHERE r.appliance_id = ? AND r.flat_id = ? AND r.timestamp_utc >= ?
                 AND r.data_quality_status IN ('valid', 'repaired')""",
            (row["appliance_id"], flat_id, start),
        ).fetchall()
        features = [item for item in active if item["energy_kwh"] > 0.015]
        candidates = appliance_candidates(connection, row["appliance_id"], flat_id)
        avg = sum(item["energy_kwh"] for item in features) / len(features) if features else 0
        home_rate = sum(1 for item in features if item["occupants_home"] and item["occupants_home"] > 0) / len(features) if features else 0
        result.append(
            {
                "appliance_id": row["appliance_id"],
                "inferred_type": row["inferred_type"],
                "confirmed_type": row["confirmed_type"],
                "effective_type": row["confirmed_type"] or row["inferred_type"],
                "inference_confidence": round(row["inference_confidence"], 3),
                "candidates": candidates,
                "weekly_kwh": round(sum(item["energy_kwh"] for item in active), 3),
                "active_hours": len(features),
                "average_active_kwh": round(avg, 3),
                "active_while_home_percent": round(home_rate * 100, 1),
            }
        )
    return result


def appliance_candidates(connection, appliance_id, flat_id):
    row = connection.execute(
        """SELECT inferred_type, inference_confidence, inference_candidates_json
           FROM appliances WHERE appliance_id = ? AND flat_id = ?""",
        (appliance_id, flat_id),
    ).fetchone()
    if not row:
        return []
    candidates = json.loads(row["inference_candidates_json"])
    if candidates:
        return candidates
    return [{"type": row["inferred_type"], "probability": row["inference_confidence"]}]


def update_appliance(connection, appliance_id, appliance_type, flat_id=None):
    if appliance_type is not None and appliance_type not in APPLIANCE_TYPES:
        raise ValueError("unsupported appliance type")
    if flat_id:
        rows = connection.execute(
            "SELECT flat_id FROM appliances WHERE appliance_id = ? AND flat_id = ?", (appliance_id, flat_id)
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT flat_id FROM appliances WHERE appliance_id = ?", (appliance_id,)
        ).fetchall()
    if not rows:
        raise KeyError("appliance not found")
    if len(rows) > 1:
        raise ValueError("flat_id is required because this circuit ID occurs in multiple flats")
    flat_id = rows[0]["flat_id"]
    connection.execute(
        """UPDATE appliances SET confirmed_type = ?, updated_at = CURRENT_TIMESTAMP
           WHERE appliance_id = ? AND flat_id = ?""",
        (appliance_type, appliance_id, flat_id),
    )
    connection.commit()
    return connection.execute(
        "SELECT * FROM appliances WHERE appliance_id = ? AND flat_id = ?", (appliance_id, flat_id)
    ).fetchone()


def _recommendations(connection, flat_id, mode="balanced"):
    usage = flat_usage(connection, flat_id, 7)
    if not usage.get("metrics"):
        return []
    start = usage["metrics"]["period_start_utc"]
    rows = connection.execute(
        """SELECT r.*, a.inferred_type, a.inference_confidence, a.confirmed_type, c.occupants_home,
                  m.spot_sek_per_kwh, m.carbon_gco2e_per_kwh
           FROM energy_readings r
           JOIN appliances a ON a.appliance_id = r.appliance_id AND a.flat_id = r.flat_id
           LEFT JOIN context_readings c
             ON c.flat_id = r.flat_id AND c.timestamp_utc = r.timestamp_utc
           LEFT JOIN flats f ON f.flat_id = r.flat_id
           LEFT JOIN hourly_market_data m
             ON m.timestamp_utc = r.timestamp_utc AND m.price_area = f.price_area
           WHERE r.flat_id = ? AND r.timestamp_utc >= ?
             AND r.data_quality_status IN ('valid', 'repaired')
           ORDER BY r.appliance_id, r.timestamp_utc""",
        (flat_id, start),
    ).fetchall()
    per_appliance = defaultdict(list)
    for row in rows:
        per_appliance[row["appliance_id"]].append(row)
    recommendations = []
    away_groups = {}
    for appliance_id, items in per_appliance.items():
        appliance = items[0]
        name = appliance["confirmed_type"] or appliance["inferred_type"]
        if name.lower() not in AWAY_REDUCE_TYPES:
            continue
        unavailable = [item for item in items if item["occupants_home"] == 0]
        unoccupied_kwh = sum(item["energy_kwh"] for item in unavailable)
        if unoccupied_kwh >= 0.08:
            is_unknown = name.lower() == "unknown appliance"
            avoidability = 0 if is_unknown else 0.5
            cost = sum(
                item["energy_kwh"] * item["spot_sek_per_kwh"]
                for item in unavailable if item["spot_sek_per_kwh"] is not None
            ) * avoidability
            carbon = sum(
                item["energy_kwh"] * item["carbon_gco2e_per_kwh"] / 1000
                for item in unavailable if item["carbon_gco2e_per_kwh"] is not None
            ) * avoidability
            group_key = name.lower()
            group = away_groups.setdefault(group_key, {
                "appliance_type": name,
                "circuit_ids": [],
                "confirmed": True,
                "confidence": appliance["inference_confidence"],
                "observed_unoccupied_kwh": 0,
                "estimated_kwh_change": 0,
                "estimated_sek_change": 0,
                "estimated_co2e_change": 0,
            })
            group["circuit_ids"].append(appliance_id)
            group["confirmed"] = group["confirmed"] and bool(appliance["confirmed_type"])
            group["confidence"] = max(group["confidence"], appliance["inference_confidence"])
            group["observed_unoccupied_kwh"] += unoccupied_kwh
            group["estimated_kwh_change"] += unoccupied_kwh * avoidability
            group["estimated_sek_change"] += max(0, cost)
            group["estimated_co2e_change"] += max(0, carbon)

    for group in away_groups.values():
        name = group["appliance_type"]
        circuit_list = ", ".join(sorted(group["circuit_ids"]))
        is_unknown = name.lower() == "unknown appliance"
        if is_unknown:
            title = "Identify unknown circuit use while away"
            explanation = (
                f"{group['observed_unoccupied_kwh']:.2f} kWh was recorded across "
                f"{len(group['circuit_ids'])} unknown circuit(s) ({circuit_list}) while nobody was home. "
                "Identify the load before considering whether any use can be reduced; no savings are assumed."
            )
            assumption = "No avoidable use is assumed until the circuit is identified."
        else:
            title = f"Check {name.lower()} use while away"
            explanation = (
                f"{group['observed_unoccupied_kwh']:.2f} kWh was recorded across "
                f"{len(group['circuit_ids'])} {name.lower()} circuit(s) ({circuit_list}) while nobody was home. "
                "The estimate assumes half of this use is avoidable; check the circuit before changing behavior."
            )
            assumption = "50% of unoccupied-period electricity is assumed avoidable."
        if group["observed_unoccupied_kwh"] < 0.08:
            continue
        recommendations.append({
            "id": f"reduce-{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}",
            "type": "reduce",
            "appliance_id": sorted(group["circuit_ids"])[0],
            "circuit_ids": sorted(group["circuit_ids"]),
            "appliance_type": name,
            "confirmed": group["confirmed"],
            "confidence": round(group["confidence"], 2),
            "title": title,
            "explanation": explanation,
            "observed_unoccupied_kwh": round(group["observed_unoccupied_kwh"], 3),
            "estimated_kwh_change": round(group["estimated_kwh_change"], 3),
            "estimated_sek_change": round(group["estimated_sek_change"], 2),
            "estimated_co2e_change": round(group["estimated_co2e_change"], 3),
            "assumption": assumption,
        })

    tomorrow = market_data(connection, flat_id, mode)
    if tomorrow["hours"]:
        market_by_hour = {item["hour"]: item for item in tomorrow["hours"]}
        for appliance_id, items in per_appliance.items():
            appliance = items[0]
            if (
                (appliance["confirmed_type"] or appliance["inferred_type"]).lower() not in ("dishwasher", "washing machine")
                or (not appliance["confirmed_type"] and appliance["inference_confidence"] < 0.30)
            ):
                continue
            runs = _recurring_appliance_runs(items)
            if not runs:
                continue
            sin_hour = sum(math.sin(2 * math.pi * run["start_hour"] / 24) for run in runs)
            cos_hour = sum(math.cos(2 * math.pi * run["start_hour"] / 24) for run in runs)
            typical_hour = round((math.atan2(sin_hour, cos_hour) * 24 / (2 * math.pi)) % 24) % 24
            typical_kwh = sum(run["energy_kwh"] for run in runs) / len(runs)
            baseline = market_by_hour[typical_hour]
            feasible = [
                item for item in tomorrow["hours"]
                if 6 <= item["hour"] <= 23
                and min(abs(item["hour"] - typical_hour), 24 - abs(item["hour"] - typical_hour)) <= 6
            ]
            if not feasible:
                continue
            destination = min(feasible, key=lambda item: item["optimization_score"])
            if baseline["optimization_score"] - destination["optimization_score"] < 0.05:
                continue
            baseline_cost = typical_kwh * baseline["spot_sek_per_kwh"]
            target_cost = typical_kwh * destination["spot_sek_per_kwh"]
            baseline_carbon = typical_kwh * baseline["carbon_gco2e_per_kwh"] / 1000
            target_carbon = typical_kwh * destination["carbon_gco2e_per_kwh"] / 1000
            cost_change = baseline_cost - target_cost
            carbon_change = baseline_carbon - target_carbon
            if baseline["hour"] == destination["hour"] or (cost_change <= 0.01 and carbon_change <= 0.001):
                continue
            name = appliance["confirmed_type"] or appliance["inferred_type"]
            recommendations.append(
                {
                    "id": f"shift-{appliance_id}-{mode}",
                    "type": "shift",
                    "mode": mode,
                    "appliance_id": appliance_id,
                    "appliance_type": name,
                    "confirmed": bool(appliance["confirmed_type"]),
                    "confidence": round(appliance["inference_confidence"], 2),
                    "title": f"Try {name.lower()} at a better time",
                    "explanation": f"Your readings show {len(runs)} short, recurring run(s) averaging {typical_kwh:.2f} kWh starting around {typical_hour:02d}:00 local time. The {mode} synthetic scenario suggests {destination['hour']:02d}:00 Sweden time, within a six-hour shift window. Shifting does not reduce energy use.",
                    "typical_kwh": round(typical_kwh, 3),
                    "usual_hour": typical_hour,
                    "usual_hour_local": typical_hour,
                    "usual_hour_utc": market_by_hour[typical_hour]["hour_utc"],
                    "suggested_hour": destination["hour"],
                    "suggested_hour_local": destination["hour_local"],
                    "suggested_hour_utc": destination["hour_utc"],
                    "runs_observed": len(runs),
                    "estimated_kwh_change": 0,
                    "estimated_sek_change": _round_nonzero(cost_change, 2),
                    "estimated_co2e_change": _round_nonzero(carbon_change, 3),
                    "assumption": "A similar load can be safely scheduled at the suggested time.",
                }
            )
    for recommendation in recommendations:
        recommendation["rank_score"] = (
            max(0, recommendation["estimated_sek_change"]) * 0.4
            + recommendation["estimated_kwh_change"] * 0.35
            + max(0, recommendation["estimated_co2e_change"]) * 0.25
        ) * recommendation["confidence"]
    return sorted(recommendations, key=lambda item: item["rank_score"], reverse=True)


def recommendations(connection, flat_id, mode="balanced"):
    if not connection.execute("SELECT 1 FROM flats WHERE flat_id = ?", (flat_id,)).fetchone():
        raise KeyError("flat not found")
    return _recommendations(connection, flat_id, mode)


def generate_weekly_summary(connection, flat_id, mode="balanced"):
    usage = flat_usage(connection, flat_id, 7)
    if not usage.get("metrics"):
        raise KeyError("flat has no usage data")
    top = _recommendations(connection, flat_id, mode)[:3]
    metrics = usage["metrics"]
    days = max(
        1,
        (datetime.strptime(metrics["period_end_utc"], "%Y-%m-%dT%H:%M:%SZ")
         - datetime.strptime(metrics["period_start_utc"], "%Y-%m-%dT%H:%M:%SZ")).days + 1,
    )
    flat = usage["flat"]
    return {
        "flat_id": flat_id,
        "flat_label": f"Flat {flat_id}",
        "period_start_utc": metrics["period_start_utc"],
        "period_end_utc": metrics["period_end_utc"],
        "energy_kwh": round(metrics["energy_kwh"], 2),
        "spot_cost_sek": round(metrics["spot_cost_sek"], 2),
        "co2e_kg": round(metrics["co2e_kg"], 3),
        "week_change_percent": metrics["week_change_percent"],
        "top_actions": top,
        "annual_projection": {
            "kwh": round(sum(item["estimated_kwh_change"] for item in top if item["type"] == "reduce") * 365 / days, 1),
            "sek": round(sum(max(0, item["estimated_sek_change"]) for item in top if item["type"] == "reduce") * 365 / days, 1),
            "co2e_kg": round(sum(max(0, item["estimated_co2e_change"]) for item in top if item["type"] == "reduce") * 365 / days, 2),
            "label": "Illustrative projection from this observed week; not measured annual savings.",
        },
        "cost_note": "Spot-energy component only; excludes retail markup, network fees, tax, VAT and fixed charges.",
        "carbon_note": "Synthetic operational emissions estimate, not lifecycle or marginal emissions.",
    }


class NotificationProvider:
    def preview(self, summary):
        raise NotImplementedError

    def send_daily(self, note):
        raise NotImplementedError


class DemoHtmlNotificationProvider(NotificationProvider):
    def preview(self, summary):
        return _email_preview_html(summary)

    def send_daily(self, note):
        return {
            "status": "simulated",
            "channel": "in_app",
            "title": note["title"],
            "message": note["message"],
        }


DEMO_NOTIFICATION_PROVIDER = DemoHtmlNotificationProvider()


def email_preview(summary):
    return DEMO_NOTIFICATION_PROVIDER.preview(summary)


def generate_daily_note(connection, flat_id, mode="balanced"):
    mode = mode.lower()
    if mode not in ("cheapest", "balanced", "greenest"):
        raise ValueError("mode must be cheapest, balanced, or greenest")
    flat = connection.execute("SELECT flat_id FROM flats WHERE flat_id = ?", (flat_id,)).fetchone()
    if not flat:
        raise KeyError("flat not found")

    markets = {choice: market_data(connection, flat_id, choice) for choice in ("cheapest", "balanced", "greenest")}
    appliances = list_appliances(connection, flat_id)
    latest = _latest_timestamp(connection, "energy_readings")
    if latest:
        start = (datetime.strptime(latest, "%Y-%m-%dT%H:%M:%SZ") - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        usage_rows = connection.execute(
            """SELECT appliance_id, energy_kwh, timestamp_utc
               FROM energy_readings
               WHERE flat_id = ? AND timestamp_utc >= ?
                 AND data_quality_status IN ('valid', 'repaired')
               ORDER BY appliance_id, timestamp_utc""",
            (flat_id, start),
        ).fetchall()
    else:
        usage_rows = []

    by_appliance = defaultdict(list)
    for row in usage_rows:
        by_appliance[row["appliance_id"]].append(row)

    flexible_types = {"dishwasher", "washing machine"}
    patterns = {}
    for appliance in appliances:
        if (
            appliance["effective_type"].lower() not in flexible_types
            or (not appliance["confirmed_type"] and appliance["inference_confidence"] < 0.30)
        ):
            continue
        runs = _recurring_appliance_runs(by_appliance[appliance["appliance_id"]])
        if not runs:
            continue
        circular_sin = sum(math.sin(2 * math.pi * run["start_hour"] / 24) for run in runs)
        circular_cos = sum(math.cos(2 * math.pi * run["start_hour"] / 24) for run in runs)
        usual_hour = round((math.atan2(circular_sin, circular_cos) * 24 / (2 * math.pi)) % 24)
        patterns[appliance["appliance_id"]] = {
            **appliance,
            "usual_hour": usual_hour % 24,
            "typical_run_kwh": sum(run["energy_kwh"] for run in runs) / len(runs),
            "runs_observed": len(runs),
        }

    mode_details = {}
    for choice, forecast in markets.items():
        actions = []
        if forecast["window"] is not None:
            hours_by_number = {item["hour"]: item for item in forecast["hours"]}
            for appliance in patterns.values():
                usual = hours_by_number[appliance["usual_hour"]]
                feasible_hours = [
                    item for item in forecast["hours"]
                    if 6 <= item["hour"] <= 23
                    and min(
                        abs(item["hour"] - appliance["usual_hour"]),
                        24 - abs(item["hour"] - appliance["usual_hour"]),
                    ) <= 6
                ]
                if not feasible_hours:
                    continue
                suggested = min(feasible_hours, key=lambda item: item["optimization_score"])
                if (
                    suggested["hour"] == usual["hour"]
                    or usual["optimization_score"] - suggested["optimization_score"] < 0.05
                ):
                    continue
                kwh = appliance["typical_run_kwh"]
                actions.append({
                    "appliance_id": appliance["appliance_id"],
                    "appliance_type": appliance["effective_type"],
                    "confirmed": bool(appliance["confirmed_type"]),
                    "confidence": appliance["inference_confidence"],
                    "runs_observed": appliance["runs_observed"],
                    "usual_hour_local": appliance["usual_hour"],
                    "suggested_hour_local": suggested["hour_local"],
                    "usual_hour_utc": usual["hour_utc"],
                    "suggested_hour_utc": suggested["hour_utc"],
                    "typical_run_kwh": round(kwh, 3),
                    "estimated_sek_change": _round_nonzero(
                        kwh * (usual["spot_sek_per_kwh"] - suggested["spot_sek_per_kwh"]), 3
                    ),
                    "estimated_co2e_change_kg": _round_nonzero(
                        kwh * (usual["carbon_gco2e_per_kwh"] - suggested["carbon_gco2e_per_kwh"]) / 1000, 4
                    ),
                    "energy_change_kwh": 0,
                    "market_at_suggested_time": {
                        "spot_sek_per_kwh": suggested["spot_sek_per_kwh"],
                        "carbon_gco2e_per_kwh": suggested["carbon_gco2e_per_kwh"],
                        "projected": suggested["projected"],
                    },
                })
        actions.sort(
            key=lambda item: {
                "cheapest": item["estimated_sek_change"],
                "greenest": item["estimated_co2e_change_kg"],
                "balanced": item["estimated_sek_change"] + item["estimated_co2e_change_kg"] * 10,
            }[choice],
            reverse=True,
        )
        mode_details[choice] = {
            "mode": choice,
            "forecast_date_local": forecast.get("date_local"),
            "forecast_kind": forecast.get("forecast_kind"),
            "as_of_utc": forecast.get("as_of_utc"),
            "is_historical_scenario": forecast.get("is_historical_scenario"),
            "window": forecast.get("window"),
            "actions": actions[:2],
        }

    selected = mode_details[mode]
    date_label = selected["forecast_date_local"] or "the next modeled day"
    historical_scenario = bool(selected["is_historical_scenario"])
    day_phrase = "Next modeled day scenario" if historical_scenario else "Tomorrow"
    mode_window_summary = []
    for choice, label in (
        ("cheapest", "cheapest"),
        ("greenest", "cleanest"),
        ("balanced", "best balance"),
    ):
        window = mode_details[choice]["window"]
        if window:
            mode_window_summary.append(f"{label} at {window['hour']:02d}:00")
    all_windows_message = (
        f"For {date_label} Sweden time (local time), the " + ", ".join(mode_window_summary[:-1])
        + (f", and {mode_window_summary[-1]} Sweden time." if len(mode_window_summary) > 1 else " Sweden time.")
        if mode_window_summary else f"No hourly market scenario is available for {date_label}."
    )
    if selected["actions"]:
        first = selected["actions"][0]
        appliance_name = first["appliance_type"].lower()
        device_advice = (
            f"{day_phrase} ({date_label}), try your {appliance_name} around "
            f"{first['suggested_hour_local']:02d}:00 Sweden time instead of its usual "
            f"{first['usual_hour_local']:02d}:00 start. Estimated change for a typical "
            f"{first['typical_run_kwh']:.2f} kWh run: "
        )
        if mode == "cheapest":
            device_advice += f"{first['estimated_sek_change']:.2f} SEK in spot-energy cost."
        elif mode == "greenest":
            device_advice += f"{first['estimated_co2e_change_kg']:.3f} kg CO₂e."
        else:
            device_advice += (
                f"{first['estimated_sek_change']:.2f} SEK and "
                f"{first['estimated_co2e_change_kg']:.3f} kg CO₂e."
            )
        device_advice += " Shifting changes timing, not kWh; confirm the appliance can safely run then."
    elif selected["window"]:
        device_advice = (
            f"{day_phrase} ({date_label}), the {mode} market window starts at "
            f"{selected['window']['hour']:02d}:00 Sweden time. No recurring dishwasher or "
            "washing-machine pattern was strong enough to suggest a specific device."
        )
    else:
        device_advice = "There is no market forecast available to make a timing suggestion."

    return {
        "flat_id": flat_id,
        "title": f"{'Next modeled day' if historical_scenario else 'Tomorrow'}'s energy nudge · {mode.title()}",
        "mode": mode,
        "forecast_date_local": date_label,
        "forecast_date_utc": (
            datetime.fromisoformat(selected["forecast_date_local"])
            .replace(tzinfo=LOCAL_TIMEZONE)
            .astimezone(timezone.utc)
            .date()
            .isoformat()
            if selected["forecast_date_local"] else None
        ),
        "forecast_kind": selected["forecast_kind"],
        "as_of_utc": selected["as_of_utc"],
        "is_historical_scenario": historical_scenario,
        "message": f"{all_windows_message} {device_advice}",
        "window": selected["window"],
        "window_summary": {
            "cheapest": mode_details["cheapest"]["window"],
            "cleanest": mode_details["greenest"]["window"],
            "balanced": mode_details["balanced"]["window"],
        },
        "actions": selected["actions"],
        "options": mode_details,
        "note": (
            "Timing suggestions use the flat's recent recurring appliance patterns and synthetic hourly forecasts. "
            "Savings are estimates relative to the usual start hour; spot cost excludes other bill components."
        ),
    }


def simulate_daily_notification(connection, flat_id, mode="balanced"):
    note = generate_daily_note(connection, flat_id, mode)
    delivery_date = datetime.now(timezone.utc).date().isoformat()
    existing = connection.execute(
        """SELECT mode, notification_json, channel, created_at
           FROM daily_notifications WHERE flat_id = ? AND delivery_date_utc = ?""",
        (flat_id, delivery_date),
    ).fetchone()
    if existing:
        return {
            "daily_note": json.loads(existing["notification_json"]),
            "delivery": {
                "status": "already_sent_today",
                "channel": existing["channel"],
                "mode": existing["mode"],
                "sent_at": existing["created_at"],
            },
        }

    delivery = DEMO_NOTIFICATION_PROVIDER.send_daily(note)
    connection.execute(
        """INSERT INTO daily_notifications
           (flat_id, delivery_date_utc, mode, notification_json, channel)
           VALUES (?, ?, ?, ?, ?)""",
        (flat_id, delivery_date, mode, json.dumps(note, ensure_ascii=False), delivery["channel"]),
    )
    connection.commit()
    row = connection.execute(
        """SELECT created_at FROM daily_notifications
           WHERE flat_id = ? AND delivery_date_utc = ?""",
        (flat_id, delivery_date),
    ).fetchone()
    return {
        "daily_note": note,
        "delivery": {
            "status": delivery["status"],
            "sent_today": True,
            "delivery_date_utc": delivery_date,
            "channel": delivery["channel"],
            "mode": mode,
            "sent_at": row["created_at"],
        },
    }


def daily_notification_status(connection, flat_id):
    if not connection.execute("SELECT 1 FROM flats WHERE flat_id = ?", (flat_id,)).fetchone():
        raise KeyError("flat not found")
    delivery_date = datetime.now(timezone.utc).date().isoformat()
    existing = connection.execute(
        """SELECT mode, channel, created_at FROM daily_notifications
           WHERE flat_id = ? AND delivery_date_utc = ?""",
        (flat_id, delivery_date),
    ).fetchone()
    if not existing:
        return {"sent_today": False, "delivery_date_utc": delivery_date}
    return {
        "sent_today": True,
        "delivery_date_utc": delivery_date,
        "mode": existing["mode"],
        "channel": existing["channel"],
        "sent_at": existing["created_at"],
    }


def _email_preview_html(summary):
    actions = "".join(
        f"<li><strong>{_escape(action['title'])}</strong><br>{_escape(action['explanation'])}</li>"
        for action in summary["top_actions"]
    ) or "<li>No high-confidence actions were identified this week.</li>"
    return (
        '<article class="email-paper"><p class="email-kicker">ENERGY COACH · WEEKLY NOTE</p>'
        f"<h2>Your energy week, { _escape(summary['flat_label']) }</h2>"
        f"<p><strong>{summary['energy_kwh']} kWh</strong> · {summary['spot_cost_sek']} SEK estimated spot cost · "
        f"{summary['co2e_kg']} kg CO₂e</p><h3>Your top actions</h3><ol>{actions}</ol>"
        f"<p class=\"email-footnote\">{_escape(summary['cost_note'])}<br>{_escape(summary['annual_projection']['label'])}</p></article>"
    )


def _escape(value):
    import html
    return html.escape(str(value))


def data_quality(connection):
    counts = {status: 0 for status in QUALITY_STATUSES}
    counts.update({row["status"]: row["count"] for row in connection.execute(
        "SELECT status, COUNT(*) AS count FROM ingestion_events GROUP BY status"
    )})
    total = sum(counts.values())
    trustworthy = counts["valid"] + counts["repaired"]
    recent = connection.execute(
        """SELECT source_reading_id, status, repairs_json, reason, created_at
           FROM ingestion_events ORDER BY id DESC LIMIT 8"""
    ).fetchall()
    return {
        "raw_readings": total,
        "valid": counts["valid"],
        "repaired": counts["repaired"],
        "suspicious": counts["suspicious"],
        "rejected": counts["rejected"],
        "trustworthy_percent": round(100 * trustworthy / total, 1) if total else 0,
        "supplemental_source_issues": connection.execute(
            "SELECT COUNT(*) FROM import_issues"
        ).fetchone()[0],
        "recent_events": [
            {
                "reading_id": row["source_reading_id"],
                "status": row["status"],
                "repairs": json.loads(row["repairs_json"]),
                "reason": row["reason"],
                "created_at": row["created_at"],
            } for row in recent
        ],
    }


def flat_list(connection):
    rows = connection.execute(
        """SELECT f.flat_id, f.residents, f.floor_area_m2, f.price_area
           FROM flats f
           GROUP BY f.flat_id ORDER BY f.flat_id"""
    ).fetchall()
    result = []
    for row in rows:
        usage = flat_usage(connection, row["flat_id"], 7)
        result.append(
            {
                **dict(row),
                "total_kwh": round(usage["metrics"].get("energy_kwh", 0), 2),
                "weekly_kwh": round(usage["metrics"].get("energy_kwh", 0), 2),
                "weekly_cost_sek": round(usage["metrics"].get("spot_cost_sek", 0), 2),
                "weekly_co2e_kg": round(usage["metrics"].get("co2e_kg", 0), 3),
            }
        )
    return result


class MeterDataProvider:
    def inject_next(self, connection):
        raise NotImplementedError


class SyntheticReplayProvider(MeterDataProvider):
    def inject_next(self, connection):
        return _replay_reading(connection)


SYNTHETIC_REPLAY_PROVIDER = SyntheticReplayProvider()


def replay_reading(connection):
    return SYNTHETIC_REPLAY_PROVIDER.inject_next(connection)


def _replay_reading(connection):
    latest = _latest_timestamp(connection, "energy_readings")
    if not latest:
        raise ValueError("Cannot replay before readings are seeded")
    stamp = (datetime.strptime(latest, "%Y-%m-%dT%H:%M:%SZ") + timedelta(hours=1)).replace(tzinfo=timezone.utc)
    timestamp = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    row = connection.execute(
        """SELECT c.*, f.price_area FROM context_readings c JOIN flats f USING (flat_id)
           WHERE c.flat_id = 'F001' ORDER BY c.timestamp_utc DESC LIMIT 1"""
    ).fetchone()
    if not row:
        raise ValueError("No context profile available for replay")
    flat = connection.execute("SELECT price_area FROM flats WHERE flat_id = 'F001'").fetchone()
    connection.execute(
        """INSERT OR IGNORE INTO context_readings
           (flat_id, timestamp_utc, indoor_temp_c, outdoor_temp_c, occupants_home)
           VALUES ('F001', ?, ?, ?, ?)""",
        (timestamp, row["indoor_temp_c"], row["outdoor_temp_c"], row["occupants_home"]),
    )
    _projected_market(connection, timestamp, flat["price_area"])
    state = connection.execute("SELECT value FROM app_state WHERE key = 'replay_count'").fetchone()
    count = int(state["value"]) + 1 if state else 1
    connection.execute(
        "INSERT OR REPLACE INTO app_state (key, value) VALUES ('replay_count', ?)",
        (str(count),),
    )
    payload = {
        "reading_id": f"DEMO-{count:04d}",
        "flat_id": "F001",
        "appliance_id": "F001-A04",
        "timestamp_utc": timestamp,
        "energy_value": "330",
        "energy_unit": " Wh ",
    }
    result = ingest_reading(connection, payload, {"F001"})
    connection.commit()
    return {"incoming": payload, "result": result, "database_timestamp_utc": timestamp}
