"""Gemini JSON calls that stay inside per-model daily budgets and fall back across models."""
import base64
import json
import time

import requests

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Daily request budget per model, kept just under the free-tier quota (requests per day, reset at
# midnight Pacific) so we never hit the hard limit. Usage is counted in Supabase (ai_usage / ai_take).
# Checked against AI Studio's rate-limit page on 2 Nov 2026: Flash models 20 a day each (3 Flash is
# served as gemini-3-flash-preview), 3.1 and 3.5 Flash-Lite 500, 2.5 Flash-Lite only 20.
BUDGETS = {
    "gemini-3.8-flash": 19,
    "gemini-3.6-flash": 19,
    "gemini-3-flash-preview": 19,
    "gemini-2.5-flash": 19,
    "gemini-3.1-flash-lite": 450,
    "gemini-3.5-flash-lite": 450,
    "gemini-2.5-flash-lite": 19,
}
# Judgement calls (classification) prefer the stronger Flash models; plain field extraction from
# documents is easy, so it goes to the high-quota Flash-Lite models first.
FLASH_FIRST = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3-flash-preview", "gemini-2.5-flash",
               "gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-2.5-flash-lite"]
LITE_FIRST = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-2.5-flash-lite",
              "gemini-3.6-flash", "gemini-3-flash-preview", "gemini-2.5-flash", "gemini-3.8-flash"]
# Requests per Flash model kept back from classification for reading scanned documents (enrich.py reads
# scans with Flash only). Since local governments were added (29 Oct 2026) classification used all of
# 3.8/3.6-flash every evening and scanned reference prices (15) waited until they dropped out.
SCAN_RESERVE = 6
SECONDS_BETWEEN_CALLS = 13  # free tier allows 5 requests per minute per model


class QuotaExhausted(RuntimeError):
    """Every model's budget for today is used up; the rest waits for the next run."""


class _ModelUnavailable(RuntimeError):
    pass


class Gemini:
    def __init__(self, api_key: str, take):
        """take(model, budget) -> bool reserves one request against today's budget (ai_take RPC)."""
        self.api_key = api_key
        self.take = take
        self.skip: set[str] = set()  # models out of budget/quota or not offered to this key

    def json(self, prompt: str, schema: dict, models: list[str], validate=None, max_output: int = 32768,
             attachments: list[tuple[str, bytes]] = (), reserve: int = 0):
        """Return (parsed_json, model). validate(parsed) may raise ValueError to reject an answer.
        attachments: (mime type, bytes) files sent with the prompt, e.g. a scanned PDF to read.
        reserve: requests of each non-Lite model's daily budget this call must leave unused."""
        errors = []
        for model in models:
            if model in self.skip:
                continue
            try:
                budget = BUDGETS[model] - (0 if "lite" in model else reserve)
                return self._call(model, prompt, schema, validate, max_output, attachments, budget=budget), model
            except _ModelUnavailable as e:
                self.skip.add(model)
                errors.append(str(e))
            except RuntimeError as e:
                errors.append(str(e))
        if all(m in self.skip for m in models):
            raise QuotaExhausted(" | ".join(errors))
        raise RuntimeError(" | ".join(errors))

    def _call(self, model, prompt, schema, validate, max_output, attachments=(), retries: int = 2, budget=None):
        config = {"temperature": 0, "responseMimeType": "application/json", "responseSchema": schema,
                  "maxOutputTokens": max_output}
        if model.startswith("gemini-2.5"):
            # 2.5 models think by default and those tokens count against the output limit, which can
            # cut a long JSON answer short. These tasks do not need it.
            config["thinkingConfig"] = {"thinkingBudget": 0}
        parts = [{"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}}
                 for mime, data in attachments] + [{"text": prompt}]
        body = {"contents": [{"parts": parts}], "generationConfig": config}

        last = None
        for _ in range(retries):
            # Every HTTP call counts against Google's quota, retries included, so reserve each one.
            if not self.take(model, BUDGETS[model] if budget is None else budget):
                raise _ModelUnavailable(f"{model}: today's budget used up")
            try:
                resp = requests.post(GEMINI_URL.format(model=model), json=body, timeout=180,
                                     headers={"x-goog-api-key": self.api_key})
                time.sleep(SECONDS_BETWEEN_CALLS)
                if resp.status_code == 429 and "PerDay" in resp.text:
                    raise _ModelUnavailable(f"{model}: Google daily quota used up")
                if resp.status_code == 404:
                    raise _ModelUnavailable(f"{model}: not available for this key")
                if resp.status_code in (429, 500, 503):
                    last = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    time.sleep(30)
                    continue
                resp.raise_for_status()
                parsed = json.loads(resp.json()["candidates"][0]["content"]["parts"][0]["text"])
                if validate:
                    validate(parsed)
                return parsed
            except (requests.RequestException, KeyError, ValueError, json.JSONDecodeError) as e:
                last = f"{type(e).__name__}: {e}"
        raise RuntimeError(f"{model} failed after {retries} attempts: {(last or '')[:300]}")


def list_models(api_key: str) -> list[str]:
    resp = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                        headers={"x-goog-api-key": api_key}, params={"pageSize": 200}, timeout=60)
    resp.raise_for_status()
    return [m["name"] for m in resp.json().get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])]
