# Pipeline Copilot

When a data pipeline breaks at 2 AM, the on-call engineer gets a wall of
logs, a failed SQL query, and very little else. Finding the root cause can
take hours, and the business dashboards stay wrong until someone does.

Pipeline Copilot takes a failed pipeline run, the error log, the SQL, the
table schemas, and the recent changes, and returns three things in under a
minute: the most likely root cause in plain English, the evidence from the
logs that supports it, and a draft fix (corrected SQL or config) the
engineer can review and apply.

Built for the Meta Global AI Developer Hackathon, Startup track, Build for
Your World theme. Solo build by Shiva Kumar Chakali, a data engineer with
10+ years of experience building Snowflake and dbt pipelines in production.

## Demo video

Watch the demo here (screen recording, no voiceover, the walkthrough
below says everything the video shows):

[Pipeline Copilot demo video](https://drive.google.com/file/d/1kS67uVYyr1wHFvIZoMWRqMNwO1p5Vuls/view?usp=drive_link)

What the video shows, step by step:

1. The home page, with the six sample failed runs in the dropdown.
2. The schema drift run (`run_01_schema_drift`): pick it, press Diagnose,
   and the app returns the root cause (schema drift), the evidence (the
   invalid identifier error line and the removed column), and the draft
   fix (update the SQL to the new column name).
3. The permission run (`run_03_permission`): same flow, root cause is a
   revoked SELECT, with the log line and the fix (restore the grant).
4. A pasted log of your own, diagnosed the same way.

If the video has no sound, nothing is lost: every result it shows is
also written out in full in "How to use it" below, and every number and
log line in it comes from the sample data in `data/runs/`.

## The honest part first

The diagnosis engine works right now, with no API key and no accounts.
It is a deterministic rule engine in Python. It reads the log, the SQL,
the schema change, and the recent changes, and classifies the failure
into one family: schema drift, bad upstream data, permission, data
quality, resource or timeout, config or dependency, or unknown. It shows
its evidence, so you can check the call yourself.

The Meta Model API part is wired up and was verified live on 2026-11-02
(endpoint `https://api.meta.ai/v1`, model `muse-spark-1.1`): with a key
set, the same evidence is sent to the Meta Model API and the model
rewrites the summary and reviews the draft fix. With no key set, the app
uses a local model that composes the summary from the engine's structured
result. The classification and the evidence never come from the model.
They always come from the local engine, so the app can never invent a
root cause.

See "Meta Model API status" below for exactly what was verified and what
is still needed.

## How to run

You only need Python 3. Nothing to install. The server uses only the
Python standard library, and the front end is plain HTML, CSS, and
JavaScript.

```bash
cd pipeline-copilot
python3 server.py
```

Then open this in your browser:

```
http://localhost:8000
```

To use a different port:

```bash
PORT=9000 python3 server.py
```

## How to use it

Pick one of the six sample failed runs from the dropdown, or paste your
own log and SQL. Press Diagnose. You get back:

- The root cause family, with a confidence level.
- A plain English summary of what happened.
- The evidence: the exact log lines, the schema change, and the SQL
  fragment that support the call.
- A draft fix: corrected SQL or config where one applies, plus the steps
  to check before applying it.
- A model review of the fix.

## Sample failed runs

Six realistic runs live in `data/runs/`. Each one has a pipeline name, a
failed step, a multi-line error log, the SQL, the schema before and
after, the recent changes, and the known correct diagnosis (used by the
tests).

| Run | Pipeline | What broke |
| --- | --- | --- |
| `run_01_schema_drift` | nightly_orders_load (Snowflake) | Upstream renamed `customer_segment` to `customer_segment_code`; the mart query still reads the old name. |
| `run_02_upstream_data` | daily_payments_staging (dbt) | The vendor feed did not arrive; the staging model found no files and loaded 0 rows. |
| `run_03_permission` | hourly_fraud_features (Snowflake) | A role cleanup revoked SELECT on `raw.payments` from the pipeline role. |
| `run_04_data_quality` | daily_orders_mart (dbt) | Duplicate `order_id` values and null `customer_id` values failed the dbt tests. |
| `run_05_config_dependency` | daily_customer_360 (dbt) | A model was renamed but the reference in `fct_customer_activity` was not updated, so the run failed to compile. |
| `run_06_resource_timeout` | weekly_revenue_rollup (Snowflake) | A full scan of 4.2 billion rows on an XSMALL warehouse timed out after 3600 seconds. |

## The API

List the sample runs:

```bash
curl http://localhost:8000/api/runs
```

Diagnose a sample run:

```bash
curl -X POST http://localhost:8000/api/diagnose \
  -H "Content-Type: application/json" \
  -d '{"run_id": "run_01_schema_drift"}'
```

Diagnose a pasted log:

```bash
curl -X POST http://localhost:8000/api/diagnose \
  -H "Content-Type: application/json" \
  -d '{"pipeline": "my_pipeline", "failed_step": "load",
       "log": "SQL compilation error: invalid identifier CUSTOMER_ID",
       "sql": "SELECT customer_id FROM orders;"}'
```

Response shape:

```json
{
  "family": "schema_drift",
  "label": "Schema drift",
  "confidence": 0.75,
  "confidence_label": "high",
  "summary": "The most likely cause is schema drift...",
  "evidence": [{"source": "log", "text": "invalid identifier 'CUSTOMER_SEGMENT'"}],
  "fix": {"title": "Fix the SQL to match the new schema", "detail": "...",
          "sql": "SELECT ...", "steps": ["...", "..."]},
  "schema_diff": [{"table": "analytics.orders_mart", "column": "customer_segment",
                   "change": "removed", "detail": "..."}],
  "model_summary": "The most likely cause is schema drift...",
  "fix_review": "Fix review: ...",
  "model_source": "local"
}
```

There is also a health check at `GET /api/health`, which reports which
model is active.

## How it is put together

| File | What it does |
| --- | --- |
| `server.py` | Web server and API. Python standard library only (`http.server`). |
| `diagnose.py` | The diagnosis engine. Scores the log, SQL, schema diff, and recent changes against the failure taxonomy, extracts evidence, and builds the draft fix. |
| `model.py` | The model layer. One interface, two implementations: a local model that composes the summary from the engine result, and a Meta Model API client that sends the same evidence to Muse Spark and returns the summary plus a fix review. |
| `test_diagnose.py` | Tests. Runs the engine over all six sample runs and checks the classification, the evidence, the fix, and the local model. |
| `data/runs/` | The six sample failed runs as JSON files. |
| `static/` | The web app: `index.html`, `style.css`, `app.js`. No frameworks, no build step. |

## Meta Model API status

What is built: `model.py` has a working Meta Model API client. It sends
the engine's structured evidence to the model with an OpenAI compatible
chat completions call and asks for a JSON reply with two fields, `summary`
and `fix_review`. It reads its credentials from the environment and is
disabled by default. Nothing in the repo contains a key.

Verified live on 2026-11-02:
- Endpoint `https://api.meta.ai/v1`, model `muse-spark-1.1`, confirmed
  with a real call against the schema drift sample run. The model
  returned a plain English summary and a fix review that correctly
  judged the draft fix (right direction, but check the replacement column
  with the upstream team and test in dev first).
- One real gotcha found and fixed in `model.py`: the model spends about
  2,100 tokens reasoning before answering, so the old 800 token limit
  cut it off and the reply came back empty. The limit is now 4096, and
  an empty reply raises a clean error instead of crashing, so the server
  falls back to the local summary.

What is still honest to say: the classification and the evidence never
come from the model. They always come from the local engine, so the app
can never invent a root cause. The model only rewrites the summary and
reviews the fix.

To wire it up, set these from the participant guide:

```bash
export META_API_KEY="your key from the participant guide"
export META_MODEL_API_BASE_URL="https://api.meta.ai/v1"
export META_MODEL_API_MODEL="muse-spark-1.1"
python3 server.py
```

If the participant guide lists a different base URL or model ID, set
those values instead. The client reads all three from the environment,
so no code change is needed. If the model call fails for any reason, the
server falls back to the local summary and says so in the response.

## Tests

```bash
python3 test_diagnose.py
```

The test runs the engine over all six sample runs, checks each one is
classified into its known correct family, checks every diagnosis has a
cause, evidence, and a complete draft fix, checks the local model
produces a summary, and checks two pasted logs (a clear permission
error, and a vague log that must come back as unknown, not a guess).

## Why this idea

Every data team loses hours to the same handful of failures: schema
drift, bad upstream data, permission changes, and silent data quality
explosions. The slow part is not fixing the problem. It is figuring out
what the problem is, from a wall of logs at 2 AM. A copilot that reads
the log, the SQL, and the schema together and points at the cause, with
the evidence in front of you, turns that search into a one minute check.

## License

MIT. See `LICENSE`. Copyright (c) 2026 Shiva Kumar Chakali.
