import csv
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from backend import energy_coach as coach


ROOT = Path(__file__).resolve().parents[1]


class EnergyCoachTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        coach.DATABASE_PATH = Path(cls.temp_dir.name) / "test.sqlite3"
        with coach.connect() as connection:
            coach.init_db(connection)
            coach.seed_dataset(connection)
            cls.initial_quality = coach.data_quality(connection)

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def test_seed_uses_shared_ingestion_and_keeps_quality_counts(self):
        with coach.connect() as connection:
            quality = coach.data_quality(connection)
            with (ROOT / "energy_readings.csv").open(newline="", encoding="utf-8-sig") as file:
                raw_count = sum(1 for _ in csv.DictReader(file))
            self.assertGreaterEqual(quality["raw_readings"], raw_count)
            self.assertEqual(
                quality["raw_readings"],
                quality["valid"] + quality["repaired"] + quality["suspicious"] + quality["rejected"],
            )
            self.assertGreater(quality["repaired"], 0)
            self.assertGreater(quality["rejected"], 0)
            self.assertGreater(quality["suspicious"], 0)
            self.assertEqual(quality["supplemental_source_issues"], 8)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM flats").fetchone()[0], 8)

    def test_normalizes_decimal_comma_wh_and_legacy_timestamp(self):
        result = coach.clean_reading(
            {
                "reading_id": "test-clean-1",
                "flat_id": "F001",
                "appliance_id": "F001-A01",
                "timestamp_utc": "2026/10/29 01:00",
                "energy_value": "330,0",
                "energy_unit": " wh ",
            },
            {"F001"},
        )
        self.assertEqual(result["status"], "repaired")
        self.assertEqual(result["reading"]["energy_kwh"], 0.33)
        self.assertEqual(result["reading"]["timestamp_utc"], "2026-10-29T01:00:00Z")
        self.assertIn("converted Wh to kWh", result["repairs"])

    def test_ambiguous_values_are_rejected_and_suspect_records_flagged(self):
        rejected = coach.clean_reading(
            {"reading_id": "bad", "flat_id": "F001", "appliance_id": "F001-A01",
             "timestamp_utc": "not-a-time", "energy_value": "", "energy_unit": "kWh"},
            {"F001"},
        )
        suspicious = coach.clean_reading(
            {"reading_id": "odd", "flat_id": "F001", "appliance_id": "F002-A01",
             "timestamp_utc": "2026-10-29T01:00:00Z", "energy_value": "-0.2", "energy_unit": "kWh"},
            {"F001"},
        )
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(suspicious["status"], "suspicious")

    def test_duplicates_are_quarantined_not_double_counted(self):
        payload = {
            "reading_id": "test-duplicate-1",
            "flat_id": "F001",
            "appliance_id": "F001-A01",
            "timestamp_utc": "2026-10-29T02:00:00Z",
            "energy_value": "0.12",
            "energy_unit": "kWh",
        }
        with coach.connect() as connection:
            first = coach.ingest_reading(connection, payload, {"F001"})
            second = coach.ingest_reading(connection, payload, {"F001"})
            connection.commit()
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "rejected")
        self.assertIn("already", second["reason"])

    def test_flat_analytics_and_market_modes_are_real_and_deterministic(self):
        with coach.connect() as connection:
            usage = coach.flat_usage(connection, "F001", 7)
            self.assertGreater(usage["metrics"]["energy_kwh"], 0)
            self.assertGreater(usage["metrics"]["spot_cost_sek"], 0)
            self.assertGreater(len(usage["series"]), 160)
            self.assertLessEqual(len(usage["series"]), 168)
            self.assertEqual(len(coach.list_appliances(connection, "F001")), 7)
            for mode in ("cheapest", "balanced", "greenest"):
                result = coach.market_data(connection, "F001", mode)
                self.assertEqual(len(result["hours"]), 24)
                self.assertEqual(result["window"]["optimization_score"], min(
                    hour["optimization_score"] for hour in result["hours"]
                ))
                for hour in result["hours"]:
                    self.assertEqual(hour["hour_local"], datetime.fromisoformat(hour["timestamp_local"]).hour)
                    self.assertEqual(hour["hour_utc"], datetime.strptime(
                        hour["timestamp_utc"], "%Y-%m-%dT%H:%M:%SZ"
                    ).hour)
                latest = connection.execute(
                    "SELECT MAX(timestamp_utc) FROM hourly_market_data WHERE is_projected = 0"
                ).fetchone()[0]
                self.assertEqual(
                    result["date_local"],
                    (coach._as_local(latest).date() + timedelta(days=1)).isoformat(),
                )
                self.assertEqual(result["date_utc"], result["hours"][0]["timestamp_utc"][:10])
                self.assertEqual(result["is_historical_scenario"], result["date_local"] < datetime.now(
                    coach.LOCAL_TIMEZONE
                ).date().isoformat())

    def test_shift_never_claims_energy_reduction(self):
        with coach.connect() as connection:
            actions = coach.recommendations(connection, "F001")
        shifts = [item for item in actions if item["type"] == "shift"]
        self.assertTrue(shifts)
        with coach.connect() as connection:
            for item in shifts:
                self.assertEqual(item["estimated_kwh_change"], 0)
                self.assertGreaterEqual(item["runs_observed"], 2)
                self.assertGreaterEqual(item["suggested_hour_local"], 6)
                self.assertLessEqual(item["suggested_hour_local"], 23)
                distance = min(
                    abs(item["suggested_hour_local"] - item["usual_hour_local"]),
                    24 - abs(item["suggested_hour_local"] - item["usual_hour_local"]),
                )
                self.assertLessEqual(distance, 6)
                market = coach.market_data(connection, "F001", item["mode"])
                hours = {hour["hour_local"]: hour for hour in market["hours"]}
                self.assertGreaterEqual(
                    hours[item["usual_hour_local"]]["optimization_score"]
                    - hours[item["suggested_hour_local"]]["optimization_score"],
                    0.05,
                )

    def test_away_reductions_only_include_discretionary_appliances(self):
        self.assertIn("portable heater", coach.AWAY_REDUCE_TYPES)
        self.assertNotIn("refrigerator", coach.AWAY_REDUCE_TYPES)
        with coach.connect() as connection:
            actions = coach.recommendations(connection, "F001")
            reductions = [item for item in actions if item["type"] == "reduce"]
            self.assertTrue(reductions)
            self.assertTrue(all(
                item["appliance_type"].lower() in coach.AWAY_REDUCE_TYPES
                for item in reductions
            ))
            self.assertFalse(any(item["appliance_type"].lower() == "refrigerator" for item in reductions))
            self.assertEqual(
                len(reductions),
                len({item["appliance_type"].lower() for item in reductions}),
            )
            dishwasher = next(item for item in reductions if item["appliance_type"].lower() == "dishwasher")
            self.assertGreaterEqual(len(dishwasher["circuit_ids"]), 2)

    def test_unknown_away_circuit_surfaces_for_identification_without_savings(self):
        appliance_id = "F001-A01"
        with coach.connect() as connection:
            coach.update_appliance(connection, appliance_id, "Unknown appliance", "F001")
            actions = coach.recommendations(connection, "F001")
            review = next(
                item for item in actions
                if item["type"] == "reduce" and item["appliance_type"] == "Unknown appliance"
            )
            self.assertIn(appliance_id, review["circuit_ids"])
            self.assertEqual(review["estimated_kwh_change"], 0)
            self.assertEqual(review["estimated_sek_change"], 0)
            self.assertIn("Identify the load", review["explanation"])
            coach.update_appliance(connection, appliance_id, None, "F001")

    def test_run_detection_requires_recurring_short_blocks_and_rejects_spikes(self):
        readings = [
            {"timestamp_utc": "2026-10-20T17:00:00Z", "energy_kwh": 0.35},
            {"timestamp_utc": "2026-10-21T17:00:00Z", "energy_kwh": 0.40},
            {"timestamp_utc": "2026-10-22T17:00:00Z", "energy_kwh": 20.0},
            {"timestamp_utc": "2026-10-23T17:00:00Z", "energy_kwh": 0.10},
        ]
        runs = coach._recurring_appliance_runs(readings)
        self.assertEqual(len(runs), 2)
        self.assertEqual({run["energy_kwh"] for run in runs}, {0.35, 0.40})

    def test_confirmed_appliance_label_overrides_inference(self):
        with coach.connect() as connection:
            appliance_id = "F001-A01"
            original = connection.execute(
                "SELECT inferred_type FROM appliances WHERE appliance_id = ?", (appliance_id,)
            ).fetchone()["inferred_type"]
            coach.update_appliance(connection, appliance_id, "Dishwasher", "F001")
            item = next(item for item in coach.list_appliances(connection, "F001") if item["appliance_id"] == appliance_id)
            self.assertEqual(item["effective_type"], "Dishwasher")
            self.assertEqual(item["inferred_type"], original)
            self.assertEqual(item["candidates"][0]["type"], original)
            self.assertAlmostEqual(item["candidates"][0]["probability"], item["inference_confidence"], places=3)
            coach.update_appliance(connection, appliance_id, None, "F001")

    def test_demo_replay_uses_ingestion_and_normalizes_wh(self):
        with coach.connect() as connection:
            replay = coach.replay_reading(connection)
            self.assertEqual(replay["result"]["status"], "accepted")
            self.assertEqual(replay["result"]["data_quality"], "repaired")
            self.assertEqual(replay["result"]["normalized_energy_kwh"], 0.33)

    def test_daily_note_uses_forecast_and_simulated_delivery_is_once_per_day(self):
        with coach.connect() as connection:
            note = coach.generate_daily_note(connection, "F001", "balanced")
            self.assertEqual(set(note["options"]), {"cheapest", "balanced", "greenest"})
            self.assertIsNotNone(note["window"])
            self.assertEqual(
                note["window_summary"]["cleanest"]["hour"],
                note["options"]["greenest"]["window"]["hour"],
            )
            for label in ("cheapest at", "cleanest at", "best balance at"):
                self.assertIn(label, note["message"])
            for mode in note["options"].values():
                for action in mode["actions"]:
                    self.assertEqual(action["energy_change_kwh"], 0)
                    self.assertIn(action["suggested_hour_utc"], range(24))
                    self.assertIn(action["suggested_hour_local"], range(6, 24))
                    self.assertGreaterEqual(action["runs_observed"], 2)
                    forecast = coach.market_data(connection, "F001", mode["mode"])
                    hours = {hour["hour_local"]: hour for hour in forecast["hours"]}
                    self.assertGreaterEqual(
                        hours[action["usual_hour_local"]]["optimization_score"]
                        - hours[action["suggested_hour_local"]]["optimization_score"],
                        0.05,
                    )
            self.assertEqual(note["forecast_date_local"], note["options"]["balanced"]["forecast_date_local"])
            self.assertEqual(
                note["is_historical_scenario"],
                note["forecast_date_local"] < datetime.now(coach.LOCAL_TIMEZONE).date().isoformat(),
            )

            first = coach.simulate_daily_notification(connection, "F001", "balanced")
            second = coach.simulate_daily_notification(connection, "F001", "cheapest")
            status = coach.daily_notification_status(connection, "F001")
            self.assertEqual(first["delivery"]["status"], "simulated")
            self.assertTrue(first["delivery"]["sent_today"])
            self.assertEqual(second["delivery"]["status"], "already_sent_today")
            self.assertEqual(second["delivery"]["mode"], "balanced")
            self.assertTrue(status["sent_today"])


if __name__ == "__main__":
    unittest.main()
