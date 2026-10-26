"""Pipeline Copilot server.

Uses only the Python standard library (http.server). No pip installs.

Run it with:
    python3 server.py
Then open:
    http://localhost:8000

Set a different port with the PORT environment variable if needed.

What it serves:
    GET  /                  the web app (static/index.html)
    GET  /static/...        CSS and JavaScript
    GET  /api/runs          list of sample failed runs
    GET  /api/runs/<id>     one sample run (without the expected answer)
    POST /api/diagnose      JSON in: {"run_id": "..."} or
                            {"log": "...", "sql": "...",
                             "schema_before": {...}, "schema_after": {...},
                             "recent_changes": [...],
                             "pipeline": "...", "failed_step": "..."}
                            JSON out: the diagnosis (family, confidence,
                            summary, evidence, fix, schema diff) plus the
                            model summary and which model produced it.
    GET  /api/health        simple health check, including which model is active

The diagnosis engine (diagnose.py) is deterministic and runs locally.
The model layer (model.py) rewrites the summary in plain English. With
no API key set it uses the local model. With META_API_KEY set it calls
the Meta Model API. See README.md for the honest status of that wiring.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from diagnose import DiagnosisEngine
from model import get_model

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(HERE, "data", "runs")
STATIC_DIR = os.path.join(HERE, "static")

ENGINE = DiagnosisEngine()


def load_runs():
    runs = {}
    for name in sorted(os.listdir(RUNS_DIR)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(RUNS_DIR, name)) as f:
            run = json.load(f)
        runs[run["id"]] = run
    return runs


RUNS = load_runs()

CONTENT_TYPES = {".html": "text/html", ".css": "text/css",
                 ".js": "application/javascript", ".json": "application/json"}


def public_run(run):
    """A sample run without the expected answer, so the UI stays honest."""
    return {k: v for k, v in run.items() if k != "expected_family"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/health":
            model = get_model()
            return self.send_json({"status": "ok", "model": model.name,
                                   "runs": len(RUNS)})
        if self.path == "/api/runs":
            listing = [{"id": r["id"], "pipeline": r["pipeline"],
                        "platform": r["platform"],
                        "failed_step": r["failed_step"]} for r in RUNS.values()]
            return self.send_json({"runs": listing})
        if self.path.startswith("/api/runs/"):
            run_id = self.path.split("/api/runs/")[1].split("/")[0]
            run = RUNS.get(run_id)
            if not run:
                return self.send_json({"error": "unknown run id"}, status=404)
            return self.send_json(public_run(run))
        path = "/static/index.html" if self.path in ("/", "/index.html") else self.path
        if not path.startswith("/static/"):
            return self.send_json({"error": "not found"}, status=404)
        file_path = os.path.join(HERE, path.lstrip("/"))
        if not os.path.isfile(file_path):
            return self.send_json({"error": "not found"}, status=404)
        ext = os.path.splitext(file_path)[1]
        with open(file_path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(ext, "text/plain"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/api/diagnose":
            return self.send_json({"error": "not found"}, status=404)
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self.send_json({"error": "request body must be JSON"}, status=400)
        if payload.get("run_id"):
            run = RUNS.get(payload["run_id"])
            if not run:
                return self.send_json({"error": "unknown run id"}, status=404)
        else:
            run = {
                "pipeline": payload.get("pipeline", "pasted run"),
                "failed_step": payload.get("failed_step", "failed step"),
                "log": payload.get("log", ""),
                "sql": payload.get("sql", ""),
                "schema_before": payload.get("schema_before", {}),
                "schema_after": payload.get("schema_after", {}),
                "recent_changes": payload.get("recent_changes", []),
            }
        if not (run.get("log") or "").strip():
            return self.send_json({"error": "please send a log to diagnose"}, status=400)
        diagnosis = ENGINE.diagnose(run)
        model = get_model()
        try:
            model_result = model.summarize(diagnosis, run)
        except Exception as exc:  # model call failed: fall back to local
            model_result = {"summary": diagnosis["summary"],
                            "fix_review": "The model call failed (%s), so this "
                                          "is the local summary." % exc,
                            "source": "local (model call failed)"}
        diagnosis["model_summary"] = model_result["summary"]
        diagnosis["fix_review"] = model_result["fix_review"]
        diagnosis["model_source"] = model_result["source"]
        diagnosis["pipeline"] = run.get("pipeline")
        diagnosis["failed_step"] = run.get("failed_step")
        return self.send_json(diagnosis)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    model = get_model()
    print("Pipeline Copilot running at http://localhost:%d" % port)
    print("Sample runs loaded: %d" % len(RUNS))
    print("Model in use: %s" % model.name)
    if model.name == "local":
        print("No META_API_KEY set, so summaries come from the local model.")
        print("Set META_API_KEY to use the Meta Model API. See README.md.")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
