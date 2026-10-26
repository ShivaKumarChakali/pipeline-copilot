"""Tests for Pipeline Copilot.

Runs the diagnosis engine over every sample run in data/runs/ and checks:
  1. The engine classifies each run into its known correct family.
  2. Every diagnosis returns a cause, at least one piece of evidence,
     and a draft fix with a title, detail, and steps.
  3. The local model produces a non-empty summary and fix review.
  4. A pasted log with no sample run attached still gets a diagnosis.

Run it with:
    python3 test_diagnose.py
Exit code 0 means every check passed.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from diagnose import DiagnosisEngine, FAMILIES
from model import LocalModel

RUNS_DIR = os.path.join(HERE, "data", "runs")

failures = []
passes = 0


def check(name, condition, detail=""):
    global passes
    if condition:
        passes += 1
        print("PASS  %s" % name)
    else:
        failures.append(name)
        print("FAIL  %s  %s" % (name, detail))


def main():
    engine = DiagnosisEngine()
    model = LocalModel()
    run_files = sorted(f for f in os.listdir(RUNS_DIR) if f.endswith(".json"))
    print("Found %d sample runs." % len(run_files))
    assert len(run_files) >= 6, "expected at least 6 sample runs"

    for name in run_files:
        with open(os.path.join(RUNS_DIR, name)) as f:
            run = json.load(f)
        expected = run["expected_family"]
        result = engine.diagnose(run)
        label = "%s (%s)" % (run["id"], run["pipeline"])
        check("%s classified as %s" % (label, expected),
              result["family"] == expected,
              "got %s, scores %s" % (result["family"], result["scores"]))
        check("%s has a cause label" % label,
              result["label"] == FAMILIES[expected])
        check("%s has evidence" % label,
              len(result["evidence"]) >= 1,
              "evidence was empty")
        check("%s has a draft fix" % label,
              bool(result["fix"]["title"]) and bool(result["fix"]["detail"])
              and len(result["fix"]["steps"]) >= 2,
              "fix was incomplete")
        check("%s confidence is not zero" % label,
              result["confidence"] > 0)
        model_result = model.summarize(result, run)
        check("%s local model summary is non-empty" % label,
              bool(model_result["summary"]) and bool(model_result["fix_review"])
              and model_result["source"] == "local")

    # A pasted log with no sample run attached: permission error, plain text.
    pasted = {
        "pipeline": "pasted run",
        "failed_step": "load",
        "log": "ERROR: permission denied for table customer_orders\n"
               "Query failed. Role ANALYST cannot read the table.",
        "sql": "SELECT * FROM customer_orders;",
        "schema_before": {},
        "schema_after": {},
        "recent_changes": [],
    }
    result = engine.diagnose(pasted)
    check("pasted permission log classified as permission",
          result["family"] == "permission",
          "got %s" % result["family"])
    check("pasted log diagnosis has evidence and fix",
          len(result["evidence"]) >= 1 and bool(result["fix"]["title"]))

    # A log with nothing recognizable must come back as unknown, not a guess.
    vague = {
        "pipeline": "pasted run",
        "failed_step": "load",
        "log": "Something went wrong at 02:00. The step stopped.",
        "sql": "",
        "schema_before": {},
        "schema_after": {},
        "recent_changes": [],
    }
    result = engine.diagnose(vague)
    check("vague log classified as unknown",
          result["family"] == "unknown",
          "got %s" % result["family"])

    print("\n%d checks passed, %d failed." % (passes, len(failures)))
    if failures:
        print("Failing checks:")
        for name in failures:
            print("  - %s" % name)
        return 1
    print("All tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
