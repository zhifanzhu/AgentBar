"""The agent bar's brain: an LLM agent that acts on the desktop through dscene (MCP).

Each request runs a tool loop: the model sees every dscene tool (plus Google
Search on Gemini), calls them, gets the results back, and finishes with a short
reply for the bar. The conversation carries over between requests until reset().

Providers:
  gemini  Google Gemini API (Interactions endpoint); history is kept server-side
  local   any OpenAI-compatible server with tool calling, e.g. the vLLM server on vast

Config lives in ~/.config/agentbar/brain.json; edit it by hand or with the bar's
/model and /key commands. GEMINI_API_KEY in the environment overrides the stored key.
"""
import asyncio
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dscene.recorder import RECORDER

CONFIG = Path.home() / ".config" / "agentbar" / "brain.json"
DEFAULTS = {
    "provider": "gemini",
    "models": {"gemini": "gemini-3.6-flash", "local": "holo3-1-9b"},
    "local_url": "http://10.0.2.2:8000/v1",
    "gemini_api_key": "",
    "grounding": {"url": "http://10.0.2.2:8000/v1", "model": "holo3-1-9b"},  # for find_element
    "max_steps": 16,
    # minimal is fastest (gemini-3.6-flash: ~1.8 s per step vs ~4.2 s for 3.8-flash at low);
    # models without it (3.8-flash, 4-argon) fall back to low automatically.
    "thinking_level": "minimal",
}
GEMINI = "https://generativelanguage.googleapis.com/v1beta"
DSCENE_URL = "http://127.0.0.1:8765/mcp"

SYSTEM = """You are the agent behind the agent bar on the user's Linux desktop (Arch Linux,
XFCE, one screen). The user types requests into the bar; you act through the dscene tools.
Show, don't tell: change what's on screen rather than writing long answers.

- Comparing options (hotels, restaurants, products, papers): research first (Google Search),
  then call set_deck with one scene per option, 2-5 scenes. For places use two panes: the
  place's own page (kind "url", a real URL from your search results) and a map (kind "map"
  with lat/lon). Put key facts in note, under 60 characters. Never invent prices or URLs;
  use a pane of kind "search" when you don't have a real URL.
- Capturing the screen into slides: select_region, then insert_image_to_slides with its path.
- How to do something in an app: bring it to the front and look at it with one set_scene
  call (e.g. {"name": "Blender", "panes": [{"kind": "blender"}]}), then call guide_step once
  per step. A step is exactly ONE click or key press: opening a menu, picking a submenu and
  picking an item are separate steps. guide_step marks the control, waits for the user and
  returns a screenshot of the result: look at it before the next step, and if the step
  didn't take effect, explain and mark it again. The user's keyboard stays in their app, so
  steps like "press Escape" work. Only say the task is complete when the screenshot shows it.
  Don't click for the user.
- "Remember this" / "go back to how it was": capture_scene returns a restorable description
  of the screen; set_scene with it brings that moment back.
- Use ask_user when the request is ambiguous. Use annotate kind "toast" only for notices in
  the middle of a task: your final reply is already shown in the bar.
- Your final text reply is shown in the bar: one short sentence."""


class BrainError(Exception):
    pass


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    if CONFIG.exists():
        stored = json.loads(CONFIG.read_text())
        for key, value in stored.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    return cfg


def save_config(cfg):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2))
    CONFIG.chmod(0o600)  # it may hold an API key


def gemini_key(cfg):
    return os.environ.get("GEMINI_API_KEY") or cfg.get("gemini_api_key", "")


def describe(cfg):
    p = cfg["provider"]
    extra = (" · key set" if gemini_key(cfg) else " · NO KEY (/key gemini …)") if p == "gemini" \
        else f" · {cfg['local_url']}"
    return f"{p} · {cfg['models'][p]}{extra}"


def _http(url, body=None, headers=None, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            err = json.loads(detail)
            err = err[0] if isinstance(err, list) else err  # Gemini wraps errors in a list
            detail = err.get("error", {}).get("message", detail)
        except (ValueError, AttributeError, IndexError):
            pass
        raise BrainError(f"HTTP {e.code}: {detail[:300]}") from None
    except urllib.error.URLError as e:
        raise BrainError(f"can't reach {urllib.parse.urlsplit(url).netloc}: {e.reason}") from None


def plain_schema(schema):
    """Inline $refs and drop JSON Schema features function-calling APIs may reject."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(defs[node["$ref"].split("/")[-1]])
        if "anyOf" in node:  # Optional[X] -> X
            options = [o for o in node["anyOf"] if o.get("type") != "null"]
            if len(options) == 1:
                merged = {**{k: v for k, v in node.items() if k != "anyOf"}, **options[0]}
                return walk(merged)
        return {k: walk(v) for k, v in node.items() if k not in ("$defs", "title", "default")}

    return walk(schema)


def _result_parts(result):
    """MCP tool result -> (text, images as (mime type, base64 data))."""
    texts, images = [], []
    for c in result.content:
        if c.type == "text":
            texts.append(c.text)
        elif c.type == "image":
            images.append((getattr(c, "mime_type", None) or getattr(c, "mimeType", "image/png"), c.data))
    text = "\n".join(texts) or ("(image)" if images else "OK")
    failed = getattr(result, "is_error", None) or getattr(result, "isError", False)
    return (f"ERROR: {text}" if failed else text), images


def _without_signatures(resp):
    """The model response minus opaque blobs (thought signatures), for the trajectory log."""
    def walk(node):
        if isinstance(node, dict):
            return {k: ("…" if k == "signature" else walk(v)) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(n) for n in node]
        return node
    return walk(resp)


class Agent:
    """One conversation with the configured model, acting through dscene."""

    def __init__(self, on_event=print):
        self.on_event = on_event  # progress lines for the bar, e.g. "→ set_deck"
        self.reset()

    def reset(self):
        self.previous_id = None  # Gemini keeps the history server-side
        self.messages = []       # OpenAI-compatible history, kept here

    def run(self, text, cfg=None):
        """Handle one request; returns the model's final reply. Blocking. Recorded as a run."""
        cfg = cfg or load_config()
        RECORDER.start(text, provider=cfg["provider"], model=cfg["models"][cfg["provider"]],
                       continues_conversation=bool(self.previous_id or self.messages))
        try:
            reply = asyncio.run(self._run(text, cfg))
        except Exception as e:
            while isinstance(e, BaseExceptionGroup) and e.exceptions:  # the MCP client wraps errors
                e = e.exceptions[0]
            RECORDER.end(error=f"{type(e).__name__}: {e}")
            raise BrainError(f"{type(e).__name__}: {e}") from e
        RECORDER.end(reply=reply)
        return reply

    async def _run(self, text, cfg):
        from mcp import Client  # imported lazily: only the brain thread needs it

        async with Client(DSCENE_URL) as dscene:
            await self._snapshot(dscene, "before")
            tools = [{"name": t.name, "description": t.description or "",
                      "parameters": plain_schema(t.input_schema)}
                     for t in (await dscene.list_tools()).tools]
            loop = self._gemini if cfg["provider"] == "gemini" else self._openai
            try:
                return await loop(cfg, text, tools, dscene)
            finally:
                await self._snapshot(dscene, "after")

    async def _snapshot(self, dscene, when):
        """Record the desktop (windows, bar state, a screenshot) without showing it to the model."""
        try:
            state = json.loads(_result_parts(await dscene.call_tool("get_scene", {}))[0])
            _, shots = _result_parts(await dscene.call_tool("get_screen", {}))
            RECORDER.event("snapshot", when=when, windows=state.pop("windows", []), state=state,
                           screenshot=RECORDER.image(shots[0][1], shots[0][0]) if shots else None)
        except Exception as e:  # noqa: BLE001 - recording must never break a request
            RECORDER.event("snapshot_failed", when=when, error=str(e))

    async def _call(self, dscene, name, args):
        """Run one dscene tool; returns (text, images)."""
        self.on_event(f"→ {name}")
        print(f"[agent] {name} {json.dumps(args)[:300]}", flush=True)
        t0 = time.perf_counter()
        try:
            text, images = _result_parts(await dscene.call_tool(name, args))
        except Exception as e:  # noqa: BLE001 - report tool failures back to the model
            text, images = f"ERROR: {e}", []
        seconds = round(time.perf_counter() - t0, 3)
        print(f"[agent]   = {text[:300]}{f' + {len(images)} image(s)' if images else ''} ({seconds}s)", flush=True)
        RECORDER.event("tool", name=name, args=args, seconds=seconds, result=text[:4000],
                       images=[RECORDER.image(data, mime) for mime, data in images])
        return text, images

    def _model_call(self, url, body, headers=None, step=0):
        """One request to the model, timed and recorded (without the images it was sent)."""
        t0 = time.perf_counter()
        resp = _http(url, body, headers)
        RECORDER.event("model", step=step, seconds=round(time.perf_counter() - t0, 3),
                       usage=resp.get("usage"), response=_without_signatures(resp))
        return resp

    async def _gemini(self, cfg, text, tools, dscene):
        key = gemini_key(cfg)
        if not key:
            raise BrainError("no Gemini API key: type /key gemini YOUR_KEY")
        declared = [{"type": "function", **t} for t in tools] + [{"type": "google_search"}]
        step_input = text
        level = cfg["thinking_level"]
        for step in range(cfg["max_steps"]):
            body = {"model": cfg["models"]["gemini"], "input": step_input, "system_instruction": SYSTEM,
                    "tools": declared, "generation_config": {"thinking_level": level}}
            if self.previous_id:
                body["previous_interaction_id"] = self.previous_id
            try:
                resp = await asyncio.to_thread(self._model_call, f"{GEMINI}/interactions", body,
                                               {"x-goog-api-key": key}, step)
            except BrainError as e:
                if "thinking" not in str(e).lower() or level == "low":
                    raise
                level = body["generation_config"]["thinking_level"] = "low"  # model lacks this level
                RECORDER.event("thinking_fallback", error=str(e))
                resp = await asyncio.to_thread(self._model_call, f"{GEMINI}/interactions", body,
                                               {"x-goog-api-key": key}, step)
            self.previous_id = resp.get("id")
            steps = resp.get("steps", [])
            calls = [s for s in steps if s.get("type") == "function_call"]
            if not calls:
                return "\n".join(c.get("text", "") for s in steps if s.get("type") == "model_output"
                                 for c in s.get("content", []) if c.get("type") == "text").strip()
            step_input = []
            for call in calls:
                text, images = await self._call(dscene, call["name"], call.get("arguments") or {})
                step_input.append({"type": "function_result", "name": call["name"], "call_id": call["id"],
                                   "result": [{"type": "text", "text": text}] + [
                                       {"type": "image", "mime_type": mime, "data": data}
                                       for mime, data in images]})
        raise BrainError(f"stopped after {cfg['max_steps']} steps")

    async def _openai(self, cfg, text, tools, dscene):
        declared = [{"type": "function", "function": t} for t in tools]
        if not self.messages:
            self.messages.append({"role": "system", "content": SYSTEM})
        self.messages.append({"role": "user", "content": text})
        for step in range(cfg["max_steps"]):
            resp = await asyncio.to_thread(self._model_call, f"{cfg['local_url']}/chat/completions", {
                "model": cfg["models"]["local"], "messages": self.messages, "tools": declared,
                "tool_choice": "auto", "temperature": 0.0, "max_tokens": 2000,
                "chat_template_kwargs": {"enable_thinking": False}}, None, step)
            msg = resp["choices"][0]["message"]
            self.messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            if not msg.get("tool_calls"):
                return (msg.get("content") or "").strip()
            for call in msg["tool_calls"]:
                try:
                    args = json.loads(call["function"]["arguments"] or "{}")
                except ValueError:
                    result = "ERROR: arguments were not valid JSON"
                else:
                    result, images = await self._call(dscene, call["function"]["name"], args)
                    if images:  # OpenAI-style tool messages carry text only
                        result += "\n[an image was returned but can't be shown to this model]"
                self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        raise BrainError(f"stopped after {cfg['max_steps']} steps")


def list_models(cfg=None):
    cfg = cfg or load_config()
    if cfg["provider"] == "local":
        return [m["id"] for m in _http(f"{cfg['local_url']}/models", timeout=10)["data"]]
    key = gemini_key(cfg)
    if not key:
        raise BrainError("no Gemini API key: type /key gemini YOUR_KEY")
    names = []
    page = ""
    while True:
        resp = _http(f"{GEMINI}/models?pageSize=200{page}", headers={"x-goog-api-key": key}, timeout=15)
        names += [m["name"].removeprefix("models/") for m in resp.get("models", [])
                  if "generateContent" in m.get("supportedGenerationMethods", [])]
        if not resp.get("nextPageToken"):
            break
        page = "&pageToken=" + resp["nextPageToken"]
    # Text models only: drop speech, image, video, embedding and live variants.
    skip = ("tts", "image", "embedding", "live", "omni", "transcribe", "veo", "imagen")
    return sorted(n for n in names if n.startswith("gemini") and not any(s in n for s in skip))
