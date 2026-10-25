"""Grounding: where on a screenshot is "the Add Modifier button"? Returns a point.

Two providers, chosen by grounding.provider in dscene's config (dscene/config.py):
  holo    Holo3.1 on an OpenAI-compatible server (e.g. vLLM on a GPU machine); ~0.6 s
  gemini  a Gemini call with the screenshot (needs a Gemini API key; costs a request)
Both answer in coordinates normalised to 0-1000 on each axis.
"""
import base64
import json
import time
import urllib.error
import urllib.request

from dscene import config

GEMINI = "https://generativelanguage.googleapis.com/v1beta"


class GroundingError(Exception):
    pass


def _post(url, body, headers=None, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise GroundingError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from None
    except urllib.error.URLError as e:
        raise GroundingError(f"can't reach {url}: {e.reason}") from None

SCHEMA = {"type": "object",
          "properties": {"x": {"type": "integer", "minimum": 0, "maximum": 1000,
                               "description": "X coordinate as integer in [0, 1000]"},
                         "y": {"type": "integer", "minimum": 0, "maximum": 1000,
                               "description": "Y coordinate as integer in [0, 1000]"}},
          "required": ["x", "y"]}


def _prompt(description):
    # H Company's element-localization prompt; it also works as a plain instruction for Gemini.
    return ("Localize an element on the GUI image according to the provided target and output a "
            f"click position.\n * You must output a valid JSON following the format: {json.dumps(SCHEMA)}\n"
            f" Your target is:\n{description}")


def _holo(cfg, png, description):
    body = {"model": cfg["model"], "temperature": 0.0, "max_tokens": 64,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}},
                {"type": "text", "text": _prompt(description)}]}],
            "structured_outputs": {"json": SCHEMA}, "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(f"{cfg['url']}/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(json.load(r)["choices"][0]["message"]["content"])


def _gemini(cfg, png, description):
    full = config.load()
    key = config.gemini_key(full)
    if not key:
        raise GroundingError("grounding provider gemini needs a Gemini API key (dscene config or GEMINI_API_KEY)")
    body = {"model": full["gemini_model"], "store": False,
            "input": [{"type": "text", "text": _prompt(description)},
                      {"type": "image", "mime_type": "image/png", "data": base64.b64encode(png).decode()}],
            "response_format": {"type": "text", "mime_type": "application/json", "schema": SCHEMA},
            "generation_config": {"thinking_level": full["thinking_level"]}}
    try:
        resp = _post(f"{GEMINI}/interactions", body, {"x-goog-api-key": key})
    except GroundingError as e:
        if "thinking" not in str(e).lower():
            raise
        body["generation_config"]["thinking_level"] = "low"  # model lacks the minimal level
        resp = _post(f"{GEMINI}/interactions", body, {"x-goog-api-key": key})
    text = [c["text"] for s in resp.get("steps", []) if s.get("type") == "model_output"
            for c in s.get("content", []) if c.get("type") == "text"]
    return json.loads(text[-1])


PROVIDERS = {"holo": _holo, "gemini": _gemini}


def localize(png, description, provider=None):
    """(x, y) normalised to 0-1000 where `description` is in the PNG, plus seconds taken."""
    cfg = config.load()["grounding"]
    provider = provider or cfg.get("provider", "holo")
    t0 = time.perf_counter()
    point = PROVIDERS[provider](cfg, png, description)
    return (point["x"], point["y"]), time.perf_counter() - t0
