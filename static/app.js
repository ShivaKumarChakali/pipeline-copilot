const runSelect = document.getElementById("runSelect");
const pipelineInput = document.getElementById("pipelineInput");
const stepInput = document.getElementById("stepInput");
const logInput = document.getElementById("logInput");
const sqlInput = document.getElementById("sqlInput");
const schemaInput = document.getElementById("schemaInput");
const changesInput = document.getElementById("changesInput");
const diagnoseBtn = document.getElementById("diagnoseBtn");
const statusLine = document.getElementById("statusLine");
const modelLine = document.getElementById("modelLine");

async function loadHealth() {
  try {
    const res = await fetch("/api/health");
    const data = await res.json();
    modelLine.textContent = data.model === "local"
      ? "Model: local (no API key set). Diagnosis works fully offline."
      : "Model: Meta Model API (" + data.model + ").";
  } catch (e) {
    modelLine.textContent = "Model status unavailable.";
  }
}

async function loadRuns() {
  const res = await fetch("/api/runs");
  const data = await res.json();
  data.runs.forEach((run) => {
    const opt = document.createElement("option");
    opt.value = run.id;
    opt.textContent = run.pipeline + " - " + run.failed_step + " (" + run.platform + ")";
    runSelect.appendChild(opt);
  });
}

runSelect.addEventListener("change", async () => {
  if (!runSelect.value) return;
  statusLine.textContent = "Loading sample run...";
  const res = await fetch("/api/runs/" + runSelect.value);
  const run = await res.json();
  pipelineInput.value = run.pipeline || "";
  stepInput.value = run.failed_step || "";
  logInput.value = run.log || "";
  sqlInput.value = run.sql || "";
  schemaInput.value = JSON.stringify(
    { before: run.schema_before || {}, after: run.schema_after || {} }, null, 2);
  changesInput.value = (run.recent_changes || []).join("\n");
  statusLine.textContent = "Sample run loaded. Press Diagnose.";
});

function showResult(d) {
  document.getElementById("resultEmpty").hidden = true;
  document.getElementById("resultBody").hidden = false;
  document.getElementById("causeLabel").textContent = d.label;
  document.getElementById("confidenceLabel").textContent =
    "confidence: " + d.confidence_label + " (" + Math.round(d.confidence * 100) + "%)";
  document.getElementById("summaryText").textContent = d.model_summary || d.summary;
  const evList = document.getElementById("evidenceList");
  evList.innerHTML = "";
  (d.evidence || []).forEach((ev) => {
    const li = document.createElement("li");
    const src = document.createElement("span");
    src.className = "src";
    src.textContent = "[" + ev.source + "] ";
    li.appendChild(src);
    li.appendChild(document.createTextNode(ev.text));
    evList.appendChild(li);
  });
  const diffBlock = document.getElementById("schemaDiffBlock");
  const diffList = document.getElementById("schemaDiffList");
  diffList.innerHTML = "";
  if (d.schema_diff && d.schema_diff.length) {
    diffBlock.hidden = false;
    d.schema_diff.forEach((item) => {
      const li = document.createElement("li");
      li.textContent = item.detail;
      diffList.appendChild(li);
    });
  } else {
    diffBlock.hidden = true;
  }
  document.getElementById("fixTitle").textContent = d.fix.title;
  document.getElementById("fixDetail").textContent = d.fix.detail;
  const fixSql = document.getElementById("fixSql");
  if (d.fix.sql) {
    fixSql.hidden = false;
    fixSql.textContent = d.fix.sql;
  } else {
    fixSql.hidden = true;
  }
  const steps = document.getElementById("fixSteps");
  steps.innerHTML = "";
  (d.fix.steps || []).forEach((step) => {
    const li = document.createElement("li");
    li.textContent = step;
    steps.appendChild(li);
  });
  document.getElementById("fixReview").textContent = d.fix_review || "";
  document.getElementById("modelSource").textContent =
    "Summary source: " + d.model_source + ". Classification and evidence come from the local engine in every case.";
}

diagnoseBtn.addEventListener("click", async () => {
  const log = logInput.value.trim();
  if (!log) {
    statusLine.textContent = "Please paste a log first, or pick a sample run.";
    return;
  }
  let schemaBefore = {};
  let schemaAfter = {};
  const schemaText = schemaInput.value.trim();
  if (schemaText) {
    try {
      const parsed = JSON.parse(schemaText);
      schemaBefore = parsed.before || parsed.schema_before || {};
      schemaAfter = parsed.after || parsed.schema_after || {};
    } catch (e) {
      statusLine.textContent = "Schema JSON did not parse. Check the JSON and try again.";
      return;
    }
  }
  const payload = {
    pipeline: pipelineInput.value.trim() || "pasted run",
    failed_step: stepInput.value.trim() || "failed step",
    log: log,
    sql: sqlInput.value,
    schema_before: schemaBefore,
    schema_after: schemaAfter,
    recent_changes: changesInput.value.split("\n").map((s) => s.trim()).filter(Boolean),
  };
  diagnoseBtn.disabled = true;
  statusLine.textContent = "Diagnosing...";
  try {
    const res = await fetch("/api/diagnose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      statusLine.textContent = data.error || "Diagnosis failed.";
      return;
    }
    showResult(data);
    statusLine.textContent = "Done in under a minute. Review the fix before applying it.";
  } catch (e) {
    statusLine.textContent = "Could not reach the server. Is it running?";
  } finally {
    diagnoseBtn.disabled = false;
  }
});

loadHealth();
loadRuns();
