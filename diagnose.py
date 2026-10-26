"""Diagnosis engine for Pipeline Copilot.

A deterministic rule engine. It takes a failed pipeline run (error log,
SQL, table schemas, recent changes) and returns a structured diagnosis:
the most likely root cause family, the evidence that supports it, and a
draft fix the engineer can review and apply.

No model call happens here. This engine is the working baseline, so the
app is fully usable with no API key. The model layer in model.py only
rewrites the plain English summary and reviews the fix; it never changes
the classification or the evidence.

Root cause families:
    schema_drift        a column was renamed, dropped, or changed type
    upstream_data       the source feed was missing, empty, or late
    permission          a role lost access to a table, schema, or warehouse
    data_quality        bad values in the data (duplicates, nulls, bad values)
    resource_timeout    the query ran out of time, memory, or warehouse size
    config_dependency   a wrong target, missing model, or bad config reference
    unknown             no family scored high enough to call it
"""

import re

FAMILIES = {
    "schema_drift": "Schema drift",
    "upstream_data": "Bad upstream data",
    "permission": "Permission / access",
    "data_quality": "Data quality",
    "resource_timeout": "Resource / timeout",
    "config_dependency": "Config / dependency",
    "unknown": "Unknown",
}

# Each rule is (pattern, weight). Patterns match against the log, the SQL,
# and the recent changes text, all lowercased.
RULES = {
    "schema_drift": [
        (r"invalid identifier", 6),
        (r"unknown column", 6),
        (r"column .* does not exist", 6),
        (r"no such column", 6),
        (r"cannot resolve column", 5),
        (r"column .* not found", 5),
        (r"invalid column name", 5),
        (r"field .* not found", 4),
        (r"type mismatch", 4),
        (r"cannot be cast", 4),
        (r"incompatible.*type", 4),
        (r"column .* renamed", 4),
        (r"column .* dropped", 4),
    ],
    "upstream_data": [
        (r"no files found", 6),
        (r"file not found", 6),
        (r"no files matched", 6),
        (r"0 rows loaded", 6),
        (r"source .* empty", 5),
        (r"no new files", 5),
        (r"upstream .* (late|delayed|missing|failed)", 5),
        (r"feed .* (late|delayed|missing)", 5),
        (r"no rows returned from source", 5),
        (r"external table.*no data", 4),
        (r"file .* not arrived", 4),
    ],
    "permission": [
        (r"insufficient privileges", 6),
        (r"permission denied", 6),
        (r"access denied", 6),
        (r"not authorized", 6),
        (r"does not have privilege", 6),
        (r"role .* revoked", 5),
        (r"authentication failed", 5),
        (r"invalid username or password", 5),
        (r"object .* does not exist or not authorized", 5),
        (r"grant .* required", 4),
        (r"403 forbidden", 4),
    ],
    "data_quality": [
        (r"duplicate", 5),
        (r"primary key violation", 6),
        (r"unique constraint", 6),
        (r"violates not-null", 6),
        (r"null value in column", 5),
        (r"check constraint", 5),
        (r"value too long", 4),
        (r"dbt test failed", 5),
        (r"test .* failed.*\d+ failure", 5),
        (r"accepted values", 4),
        (r"row count (dropped|fell)", 4),
        (r"data quality", 4),
        (r"anomaly detected", 4),
        (r"unexpected null", 4),
    ],
    "resource_timeout": [
        (r"timed out", 6),
        (r"statement timed out", 6),
        (r"query.*cancelled", 5),
        (r"out of memory", 6),
        (r"memory limit", 5),
        (r"warehouse .* suspended", 5),
        (r"warehouse.*too small", 4),
        (r"exceeded.*(memory|time|limit)", 4),
        (r"spilling to disk", 3),
        (r"query.*too large", 4),
    ],
    "config_dependency": [
        (r"compilation error", 5),
        (r"depends on a node.*not found", 6),
        (r"model .* not found", 5),
        (r"relation .* does not exist", 5),
        (r"table .* does not exist", 4),
        (r"undefined variable", 5),
        (r"macro .* not found", 5),
        (r"syntax error", 4),
        (r"invalid reference", 4),
        (r"package .* not found", 5),
        (r"version mismatch", 4),
        (r"target .* not found", 4),
        (r"database .* does not exist", 4),
    ],
}


def _schema_diff(before, after):
    """Compare before/after schemas. Returns a list of diff dicts.

    Schemas are dicts of table name to dict of column name to type.
    """
    diffs = []
    tables = set(before) | set(after)
    for table in sorted(tables):
        bcols = before.get(table, {})
        acols = after.get(table, {})
        for col in sorted(set(bcols) - set(acols)):
            diffs.append({"table": table, "column": col, "change": "removed",
                          "detail": "Column %s was removed from %s (was %s)."
                                    % (col, table, bcols[col])})
        for col in sorted(set(acols) - set(bcols)):
            diffs.append({"table": table, "column": col, "change": "added",
                          "detail": "Column %s was added to %s as %s."
                                    % (col, table, acols[col])})
        for col in sorted(set(bcols) & set(acols)):
            if bcols[col] != acols[col]:
                diffs.append({"table": table, "column": col, "change": "type_changed",
                              "detail": "Column %s in %s changed type from %s to %s."
                                        % (col, table, bcols[col], acols[col])})
    return diffs


def _find_sql_columns(sql):
    """Return the set of column-ish identifiers used in the SQL, lowercased."""
    text = sql or ""
    found = set()
    for m in re.finditer(r"[\w]+\.([\w]+)", text):
        found.add(m.group(1).lower())
    for m in re.finditer(r"\bselect\s+([\w\s,]+?)\s+from\b", text, re.I | re.S):
        for part in m.group(1).split(","):
            part = part.strip().split()[-1] if part.strip() else ""
            if part:
                found.add(part.lower())
    return found


def _find_sql_tables(sql):
    text = sql or ""
    tables = set()
    for m in re.finditer(r"\b(?:from|join)\s+([\w\.]+)", text, re.I):
        tables.add(m.group(1).lower())
    return tables


class DiagnosisEngine:
    def diagnose(self, run):
        log = run.get("log", "") or ""
        sql = run.get("sql", "") or ""
        changes = run.get("recent_changes", []) or []
        before = run.get("schema_before", {}) or {}
        after = run.get("schema_after", {}) or {}
        changes_text = "\n".join(str(c) for c in changes)

        blob = "\n".join([log, sql, changes_text]).lower()
        log_lines = [ln.strip() for ln in log.splitlines() if ln.strip()]

        scores = {fam: 0 for fam in FAMILIES if fam != "unknown"}
        evidence = []

        def add_evidence(source, text):
            item = {"source": source, "text": text}
            if item not in evidence:
                evidence.append(item)

        # 1. Pattern rules over log, SQL, and recent changes.
        for family, rules in RULES.items():
            for pattern, weight in rules:
                for line in log_lines:
                    if re.search(pattern, line.lower()):
                        scores[family] += weight
                        add_evidence("log", line)
                if re.search(pattern, sql.lower()):
                    scores[family] += weight
                if re.search(pattern, changes_text.lower()):
                    scores[family] += weight

        # 2. Schema diff signals. A column the SQL uses that vanished from
        #    the schema is the strongest schema drift signal there is.
        diffs = _schema_diff(before, after)
        sql_cols = _find_sql_columns(sql)
        sql_tables = _find_sql_tables(sql)
        missing_cols = []
        for d in diffs:
            if d["change"] in ("removed", "type_changed") and d["column"].lower() in sql_cols:
                scores["schema_drift"] += 6 if d["change"] == "removed" else 4
                add_evidence("schema", d["detail"])
                if d["change"] == "removed":
                    missing_cols.append(d["column"])
            elif d["change"] == "removed":
                scores["schema_drift"] += 2
                add_evidence("schema", d["detail"])
            elif d["change"] == "type_changed":
                scores["schema_drift"] += 2
                add_evidence("schema", d["detail"])
            elif d["change"] == "added":
                add_evidence("schema", d["detail"])
        for table in sql_tables:
            short = table.split(".")[-1]
            if short not in {t.lower() for t in after} and after:
                scores["config_dependency"] += 3
                add_evidence("sql", "SQL reads from %s, which is not in the current schema." % table)

        # 3. Recent change signals. A change that lines up with a family
        #    is strong corroboration, not a guess on its own.
        change_blob = changes_text.lower()
        if any(w in change_blob for w in ("renamed", "dropped column", "type change", "column removed")):
            scores["schema_drift"] += 4
            add_evidence("change", next(c for c in changes if any(
                w in str(c).lower() for w in ("renamed", "dropped column", "type change", "column removed"))))
        if any(w in change_blob for w in ("role", "grant", "revoked", "permission", "access")):
            scores["permission"] += 4
        if any(w in change_blob for w in ("source", "vendor", "feed", "sftp")):
            scores["upstream_data"] += 3
        if any(w in change_blob for w in ("dbt version", "package", "macro", "target", "renamed model")):
            scores["config_dependency"] += 3
        if any(w in change_blob for w in ("warehouse size", "warehouse resized", "suspended")):
            scores["resource_timeout"] += 3

        # 4. Pick the winner.
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best_family, best_score = ranked[0]
        total = sum(scores.values())
        if best_score < 4:
            family = "unknown"
            confidence = 0.0
        else:
            family = best_family
            confidence = round(best_score / total, 2) if total else 0.0
        if confidence >= 0.6:
            confidence_label = "high"
        elif confidence >= 0.4:
            confidence_label = "medium"
        else:
            confidence_label = "low"

        fix = self._build_fix(family, run, missing_cols, sql)
        summary = self._build_summary(family, run, evidence, best_score)
        return {
            "family": family,
            "label": FAMILIES[family],
            "confidence": confidence,
            "confidence_label": confidence_label,
            "summary": summary,
            "evidence": evidence[:6],
            "fix": fix,
            "schema_diff": diffs,
            "scores": {k: v for k, v in ranked if v > 0},
        }

    # ---------- fix builders ----------

    def _first_table(self, sql):
        tables = _find_sql_tables(sql)
        return sorted(tables)[0] if tables else "the target table"

    def _build_fix(self, family, run, missing_cols, sql):
        table = self._first_table(sql)
        if family == "schema_drift":
            if missing_cols:
                col = missing_cols[0]
                corrected = self._sql_without_column(sql, col)
                return {
                    "title": "Fix the SQL to match the new schema",
                    "detail": ("The column %s no longer exists. Either use its replacement "
                               "column, or add the column back. The corrected query below "
                               "drops the missing column so the run can finish, then check "
                               "with the upstream team which replacement column is correct."
                               % col),
                    "sql": corrected,
                    "steps": [
                        "Confirm the replacement column with the team that owns the source table.",
                        "Update the model or query to use the new column name.",
                        "Add a schema change alert so the next rename fails fast instead of at 2 AM.",
                    ],
                }
            return {
                "title": "Reconcile the schema before rerunning",
                "detail": "A column changed type or was removed. Compare the before and after schema in the evidence, update the query, and rerun.",
                "sql": None,
                "steps": [
                    "Diff the source schema against the last good run.",
                    "Update the query or the staging model to match.",
                    "Add a schema drift check to the pipeline.",
                ],
            }
        if family == "permission":
            return {
                "title": "Restore the missing grant",
                "detail": ("The role running this pipeline lost access to a table it needs. "
                           "Re-grant access, then rerun the failed step."),
                "sql": "GRANT SELECT ON TABLE %s TO ROLE <pipeline_role>;" % table,
                "steps": [
                    "Find which role the pipeline runs as and what it lost access to.",
                    "Re-run the GRANT above with a role that has grant privileges.",
                    "Ask the security team to add the grant to the role change checklist.",
                ],
            }
        if family == "upstream_data":
            return {
                "title": "Wait for the source feed, then backfill",
                "detail": ("The source data did not arrive or arrived empty. Do not rerun "
                           "until the feed lands, or the run will fail the same way."),
                "sql": ("SELECT COUNT(*) AS rows_arrived\nFROM %s\n"
                        "WHERE loaded_at >= CURRENT_DATE();" % table),
                "steps": [
                    "Check the source feed status with the vendor or upstream team.",
                    "Run the count query above to confirm the data has landed.",
                    "Rerun the failed step, then add a freshness check that fails fast when the feed is late.",
                ],
            }
        if family == "data_quality":
            return {
                "title": "Quarantine the bad rows and fix the source",
                "detail": ("Bad values reached a table that expects clean data. Quarantine "
                           "the failing rows so the good rows can load, then fix the source."),
                "sql": ("SELECT *\nFROM %s\nWHERE <key_column> IS NULL\n   OR <key_column> IN (\n"
                        "  SELECT <key_column> FROM %s\n  GROUP BY <key_column> HAVING COUNT(*) > 1\n);" % (table, table)),
                "steps": [
                    "Run the quarantine query to see the bad rows.",
                    "Load the good rows, and send the bad rows back to the source team.",
                    "Add a not-null and uniqueness test on the key column so this fails in staging next time.",
                ],
            }
        if family == "resource_timeout":
            return {
                "title": "Give the query more room, then trim the work",
                "detail": ("The query ran out of time or memory. Scale the warehouse up for "
                           "this run, and narrow the query so it does not need to scan everything."),
                "sql": ("ALTER WAREHOUSE <warehouse_name> SET WAREHOUSE_SIZE = 'LARGE';\n"
                        "-- Then rerun with a date filter, for example:\n"
                        "-- WHERE loaded_at >= CURRENT_DATE - 7"),
                "steps": [
                    "Scale the warehouse up one size for the rerun.",
                    "Add a date filter or partition filter to the query.",
                    "If it still times out, split the load into smaller batches.",
                ],
            }
        if family == "config_dependency":
            return {
                "title": "Fix the broken reference, then compile before running",
                "detail": ("The pipeline points at a model, table, or target that does not "
                           "exist under that name. Fix the reference, run a compile check, "
                           "then rerun."),
                "sql": None,
                "steps": [
                    "Find the renamed or moved model and update the reference to its current name.",
                    "Run dbt compile (or the equivalent check) before the next full run.",
                    "Add the compile check to CI so a bad reference never reaches production.",
                ],
            }
        return {
            "title": "Narrow it down by hand",
            "detail": ("No failure family scored high enough to call it. Start from the "
                       "first error line in the log and check the three usual suspects: "
                       "schema, access, and the source feed."),
            "sql": None,
            "steps": [
                "Read the first error line, not the last one.",
                "Check whether the source schema changed in the last 24 hours.",
                "Check whether the pipeline role still has access to every table it reads.",
            ],
        }

    def _sql_without_column(self, sql, column):
        lines = []
        for line in (sql or "").splitlines():
            if column.lower() in line.lower() and line.strip().lower().startswith(("select", "  ", "    ")):
                continue
            lines.append(line)
        fixed = "\n".join(lines)
        if fixed.strip() == (sql or "").strip():
            fixed = sql + "\n-- NOTE: remove the reference to %s, which no longer exists." % column
        return fixed

    def _build_summary(self, family, run, evidence, score):
        pipeline = run.get("pipeline", "this pipeline")
        step = run.get("failed_step", "the failed step")
        if family == "schema_drift":
            return ("The most likely cause is schema drift in %s. A column the query "
                    "reads was renamed, dropped, or changed type, so the warehouse "
                    "rejected the SQL at step '%s'. The evidence below shows the exact "
                    "error line and the schema change." % (pipeline, step))
        if family == "permission":
            return ("The most likely cause is a permission problem in %s. The role "
                    "running step '%s' lost access to a table it needs, so the "
                    "warehouse refused the query. The error line in the evidence "
                    "names the object it could not touch." % (pipeline, step))
        if family == "upstream_data":
            return ("The most likely cause is bad upstream data for %s. The source feed "
                    "did not arrive or arrived empty, so step '%s' had nothing to "
                    "load. Rerunning now would fail the same way; the feed has to "
                    "land first." % (pipeline, step))
        if family == "data_quality":
            return ("The most likely cause is a data quality problem in %s. Bad values "
                    "reached a table that expects clean data, and the load or the "
                    "quality test at step '%s' caught them. The evidence shows the "
                    "failing test and the kind of bad rows involved." % (pipeline, step))
        if family == "resource_timeout":
            return ("The most likely cause is a resource or timeout problem in %s. "
                    "The query at step '%s' ran longer than the warehouse allowed, "
                    "or ran out of memory. It needs a bigger warehouse, a narrower "
                    "query, or both." % (pipeline, step))
        if family == "config_dependency":
            return ("The most likely cause is a config or dependency problem in %s. "
                    "Step '%s' points at a model, table, or target that does not "
                    "exist under that name, so the run failed before it could do "
                    "any real work." % (pipeline, step))
        return ("No failure family scored high enough to call it for %s. The log "
                "does not match the usual patterns, so this one needs a human to "
                "read the first error line and check schema, access, and the "
                "source feed in that order." % pipeline)
