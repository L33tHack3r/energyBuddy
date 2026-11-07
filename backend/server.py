import json
import mimetypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from energy_coach import (
    DATABASE_PATH,
    connect,
    data_quality,
    daily_notification_status,
    email_preview,
    flat_list,
    flat_usage,
    generate_daily_note,
    generate_weekly_summary,
    infer_all_appliances,
    init_db,
    ingest_reading,
    list_appliances,
    market_data,
    recommendations,
    replay_reading,
    seed_dataset,
    simulate_daily_notification,
    update_appliance,
)


ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"


class APIHandler(BaseHTTPRequestHandler):
    server_version = "EnergyCoach/1.0"

    def log_message(self, format_string, *args):
        print(f"{self.address_string()} - {format_string % args}")

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid Content-Length")
        if length > 1_000_000:
            raise ValueError("request body exceeds 1 MB")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("request body must be valid JSON")

    def _serve_static(self, filepath):
        if not filepath.is_file():
            self.send_error(404)
            return
        body = filepath.read_bytes()
        content_type = mimetypes.guess_type(str(filepath))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        params = parse_qs(parsed.query)
        if path == "/":
            return self._serve_static(FRONTEND_DIR / "index.html")
        if path in ("/app.css", "/app.js"):
            return self._serve_static(FRONTEND_DIR / path.lstrip("/"))
        try:
            with connect() as connection:
                if path == "/api/health":
                    return self._json(200, {"status": "ok", "database": str(DATABASE_PATH.name)})
                if path == "/api/flats":
                    return self._json(200, {"flats": flat_list(connection)})
                if path == "/api/data-quality":
                    return self._json(200, data_quality(connection))
                if path == "/api/market/tomorrow":
                    flats = connection.execute("SELECT flat_id FROM flats ORDER BY flat_id").fetchall()
                    flat_id = params.get("flat_id", [flats[0]["flat_id"] if flats else ""])[0]
                    mode = params.get("mode", ["balanced"])[0]
                    return self._json(200, market_data(connection, flat_id, mode))
                match = re.fullmatch(r"/api/flats/([^/]+)", path)
                if match:
                    flat_id = match.group(1)
                    flat = connection.execute("SELECT * FROM flats WHERE flat_id = ?", (flat_id,)).fetchone()
                    if flat is None:
                        return self._json(404, {"error": "flat not found"})
                    return self._json(200, {"flat": dict(flat)})
                match = re.fullmatch(r"/api/flats/([^/]+)/usage", path)
                if match:
                    days = int(params.get("days", ["7"])[0])
                    return self._json(200, flat_usage(connection, match.group(1), min(max(days, 1), 28)))
                match = re.fullmatch(r"/api/flats/([^/]+)/appliances", path)
                if match:
                    return self._json(200, {"appliances": list_appliances(connection, match.group(1))})
                match = re.fullmatch(r"/api/flats/([^/]+)/recommendations", path)
                if match:
                    mode = params.get("mode", ["balanced"])[0]
                    return self._json(200, {"recommendations": recommendations(connection, match.group(1), mode)})
                match = re.fullmatch(r"/api/flats/([^/]+)/weekly-summary", path)
                if match:
                    mode = params.get("mode", ["balanced"])[0]
                    summary = generate_weekly_summary(connection, match.group(1), mode)
                    return self._json(200, {"summary": summary, "email_html": email_preview(summary)})
                match = re.fullmatch(r"/api/flats/([^/]+)/daily-note", path)
                if match:
                    flat_id = match.group(1)
                    mode = params.get("mode", ["balanced"])[0]
                    return self._json(
                        200,
                        {
                            "daily_note": generate_daily_note(connection, flat_id, mode),
                            "delivery": daily_notification_status(connection, flat_id),
                        },
                    )
                return self._json(404, {"error": "route not found"})
        except KeyError as error:
            return self._json(404, {"error": str(error).strip("'")})
        except ValueError as error:
            return self._json(400, {"error": str(error)})
        except Exception as error:
            print(f"GET {path} failed: {error}")
            return self._json(500, {"error": "internal server error"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            payload = self._body()
            with connect() as connection:
                if path == "/api/readings":
                    if not isinstance(payload, dict):
                        raise ValueError("request body must be an object")
                    known = {row["flat_id"] for row in connection.execute("SELECT flat_id FROM flats")}
                    result = ingest_reading(connection, payload, known)
                    connection.commit()
                    if result["status"] == "accepted":
                        infer_all_appliances(connection)
                    status = 202 if result["status"] == "accepted" else 422
                    return self._json(status, result)
                if path == "/api/readings/batch":
                    readings = payload.get("readings") if isinstance(payload, dict) else payload
                    if not isinstance(readings, list):
                        raise ValueError("batch body must be a list or {readings: [...]}")
                    if len(readings) > 500:
                        raise ValueError("batch is limited to 500 readings")
                    known = {row["flat_id"] for row in connection.execute("SELECT flat_id FROM flats")}
                    results = [
                        ingest_reading(connection, item, known)
                        for item in readings
                    ]
                    connection.commit()
                    if any(item["status"] == "accepted" for item in results):
                        infer_all_appliances(connection)
                    return self._json(
                        200,
                        {
                            "accepted": sum(item["status"] == "accepted" for item in results),
                            "rejected": sum(item["status"] == "rejected" for item in results),
                            "results": results,
                        },
                    )
                if path == "/api/demo/replay":
                    result = replay_reading(connection)
                    infer_all_appliances(connection)
                    return self._json(201, result)
                match = re.fullmatch(r"/api/flats/([^/]+)/daily-notification", path)
                if match:
                    mode = payload.get("mode", "balanced") if isinstance(payload, dict) else "balanced"
                    return self._json(
                        201,
                        simulate_daily_notification(connection, match.group(1), mode),
                    )
                if path.startswith("/api/flats/") and path.endswith("/recommendations/generate"):
                    match = re.fullmatch(r"/api/flats/([^/]+)/recommendations/generate", path)
                    if not match:
                        return self._json(404, {"error": "route not found"})
                    mode = payload.get("mode", "balanced") if isinstance(payload, dict) else "balanced"
                    return self._json(200, {"recommendations": recommendations(connection, match.group(1), mode)})
                return self._json(404, {"error": "route not found"})
        except ValueError as error:
            return self._json(400, {"error": str(error)})
        except KeyError as error:
            return self._json(404, {"error": str(error).strip("'")})
        except Exception as error:
            print(f"POST {path} failed: {error}")
            return self._json(500, {"error": "internal server error"})

    def do_PATCH(self):
        path = urlparse(self.path).path
        match = re.fullmatch(r"/api/appliances/([^/]+)", path)
        flat_match = re.fullmatch(r"/api/flats/([^/]+)/appliances/([^/]+)", path)
        if not match and not flat_match:
            return self._json(404, {"error": "route not found"})
        try:
            payload = self._body()
            if not isinstance(payload, dict) or "confirmed_type" not in payload:
                raise ValueError("confirmed_type is required; use null to clear confirmation")
            with connect() as connection:
                appliance_id = unquote(flat_match.group(2) if flat_match else match.group(1))
                flat_id = unquote(flat_match.group(1)) if flat_match else payload.get("flat_id")
                row = update_appliance(connection, appliance_id, payload["confirmed_type"], flat_id)
                return self._json(
                    200,
                    {
                        "appliance_id": row["appliance_id"],
                        "flat_id": row["flat_id"],
                        "confirmed_type": row["confirmed_type"],
                        "effective_type": row["confirmed_type"] or row["inferred_type"],
                    },
                )
        except ValueError as error:
            return self._json(400, {"error": str(error)})
        except KeyError as error:
            return self._json(404, {"error": str(error).strip("'")})
        except Exception as error:
            print(f"PATCH {path} failed: {error}")
            return self._json(500, {"error": "internal server error"})


def main():
    with connect() as connection:
        init_db(connection)
        seed_dataset(connection)
    host = "0.0.0.0"
    port = int(__import__("os").environ.get("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), APIHandler)
    print(f"Energy Coach API listening on http://{host}:{port} (database: {DATABASE_PATH})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Energy Coach")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
