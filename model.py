"""Model layer for Pipeline Copilot.

Two implementations behind one interface:

    LocalModel
        No API key needed. Composes the plain English summary and the fix
        review from the diagnosis engine's structured result. This is the
        default, and it is what the app uses today.

    MetaModelClient
        Calls the Meta Model API (Muse Spark) with the same structured
        evidence, and asks the model to rewrite the summary in plain
        English and review the draft fix. Disabled unless an API key is
        set in the environment.

What was verified about the Meta Model API (checked 2026-10-26):
  - Meta's own product page (developer.meta.com, Meta Model API) says it
    gives direct access to Muse Spark, with a one-click API key, and that
    you point existing OpenAI SDK code at it. That page does not list a
    base URL or model IDs in the text I could read.
  - The LiteLLM and Promptfoo provider docs, both independent third
    parties, agree on the details: base URL https://api.meta.ai/v1,
    OpenAI compatible POST /chat/completions, key in the META_API_KEY
    environment variable, model IDs muse-spark-1.1 and muse-spark-1.3.
  - I have NOT verified these defaults against Meta's own docs or the
    hackathon participant guide, and I have no API key. So the defaults
    below are marked as third party sourced, and every value is
    overridable from the environment. Confirm the base URL and model ID
    from the participant guide before trusting a live call.

Environment variables:
    META_API_KEY            the Meta Model API key (also accepts MODEL_API_KEY)
    META_MODEL_API_BASE_URL override, default https://api.meta.ai/v1
    META_MODEL_API_MODEL    override, default muse-spark-1.1

The client never sends credentials anywhere except the configured API
base URL, and never logs the key.
"""

import json
import os
import urllib.request
import urllib.error

# Defaults below come from third party provider docs (LiteLLM, Promptfoo),
# not from Meta's own docs. Confirm against the participant guide.
DEFAULT_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark-1.1"


class LocalModel:
    """Plain English summary and fix review, built from the engine result."""

    name = "local"

    def summarize(self, diagnosis, run):
        fix = diagnosis["fix"]
        summary = diagnosis["summary"]
        review = (
            "Fix review: %s %s "
            "Check the evidence first. If the log lines and the schema change "
            "agree with the cause above, the fix is safe to try in staging. "
            "Do not apply it to production without a review, and rerun the "
            "failed step after applying it."
            % (fix["title"], fix["detail"])
        )
        return {"summary": summary, "fix_review": review, "source": "local"}


class MetaModelClient:
    """Meta Model API client (OpenAI compatible chat completions)."""

    name = "meta-model-api"

    def __init__(self, api_key=None, base_url=None, model=None):
        self.api_key = api_key or os.environ.get("META_API_KEY") or os.environ.get("MODEL_API_KEY")
        self.base_url = (base_url or os.environ.get("META_MODEL_API_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("META_MODEL_API_MODEL") or DEFAULT_MODEL

    @property
    def enabled(self):
        return bool(self.api_key)

    def _prompt(self, diagnosis, run):
        evidence_text = "\n".join(
            "- [%s] %s" % (e["source"], e["text"]) for e in diagnosis["evidence"])
        fix = diagnosis["fix"]
        return (
            "You are a senior data engineer reviewing a failed pipeline run. "
            "The diagnosis engine classified the root cause as: %s "
            "(confidence %s, %.0f%%). "
            "Pipeline: %s. Failed step: %s.\n\n"
            "Evidence:\n%s\n\n"
            "Draft fix: %s. %s\n\n"
            "Reply as JSON with two fields: \"summary\" (the root cause in "
            "plain English, two or three sentences, no jargon) and "
            "\"fix_review\" (one short paragraph: is the draft fix correct, "
            "and what should the engineer check before applying it?)."
            % (diagnosis["label"], diagnosis["confidence_label"],
               diagnosis["confidence"] * 100, run.get("pipeline", "unknown"),
               run.get("failed_step", "unknown"), evidence_text,
               fix["title"], fix["detail"])
        )

    def summarize(self, diagnosis, run):
        if not self.enabled:
            raise RuntimeError("Meta Model API key is not set. "
                               "Set META_API_KEY to enable the Meta Model API.")
        body = {
            "model": self.model,
            "messages": [
                {"role": "system",
                 "content": "You review failed data pipeline runs. Reply only with JSON."},
                {"role": "user", "content": self._prompt(diagnosis, run)},
            ],
            "temperature": 0.2,
            "max_tokens": 800,
        }
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError("Meta Model API returned HTTP %d: %s"
                               % (exc.code, detail))
        except urllib.error.URLError as exc:
            raise RuntimeError("Could not reach the Meta Model API: %s" % exc.reason)
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("Unexpected response shape from the Meta Model API.")
        parsed = _parse_json_reply(text)
        return {
            "summary": parsed.get("summary") or text,
            "fix_review": parsed.get("fix_review") or "",
            "source": "meta-model-api",
            "model": self.model,
        }


def _parse_json_reply(text):
    """Parse a model reply that should be JSON, tolerating code fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    return {}


def get_model():
    """Return the Meta Model API client when a key is set, else the local model."""
    client = MetaModelClient()
    if client.enabled:
        return client
    return LocalModel()
