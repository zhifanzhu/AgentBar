"""SceneBar's brain: an LLM agent that acts on the desktop through dscene (MCP).

Each request runs a tool loop: the model sees every dscene tool (plus Google
Search on Gemini), calls them, gets the results back, and finishes with a short
reply for the bar. The conversation carries over between requests until reset().

Providers:
  gemini  Google Gemini API (Interactions endpoint, store: false); the history is kept here
          and sent with each call
  local   any OpenAI-compatible server with tool calling, e.g. the vLLM server on vast

Config lives in ~/.config/scenebar/brain.json; edit it by hand or with the bar's
/model and /key commands. GEMINI_API_KEY in the environment overrides the stored key.
"""
import asyncio
import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dscene import config as dscene_config
from recorder import RECORDER

CONFIG = Path.home() / ".config" / "scenebar" / "brain.json"
DEFAULTS = {
    "provider": "gemini",
    "models": {"gemini": "gemini-3.6-flash", "local": "holo3-1-9b"},
    "local_url": "http://10.0.2.2:8000/v1",
    "gemini_api_key": "",
    "max_steps": 16,
    # minimal is fastest (gemini-3.6-flash: ~1.8 s per step vs ~4.2 s for 3.8-flash at low);
    # models without it (3.8-flash, 4-argon) fall back to low automatically.
    "thinking_level": "minimal",
}
GEMINI = "https://generativelanguage.googleapis.com/v1beta"
DSCENE_URL = "http://127.0.0.1:8765/mcp"
THINKING_FALLBACK = {}  # model -> level it needs instead of the configured one (learned once)
# Tools that do the user's task for them; /show (teaching mode) refuses them.
DOES_IT_FOR_USER = {"blender_run", "insert_image_to_slides"}
MODES = {  # bar commands that put one request in a mode: the note the model gets with it
    "teach": "[Teaching mode] Show me how, step by step with guide_step, so I do it myself. "
             "Don't do it for me.",
    "deck": "[Deck mode] Compare options: research, then call set_deck with 2-5 scenes, one per "
            "option.",
}
DECK_TOOLS = {"set_deck", "save_deck", "swap_scene"}  # offered to the model only in deck mode (/deck)
# Requests that sound like they want options to pick from: outside deck mode, the reply
# mentions /deck.
WANTS_OPTIONS = re.compile(r"\b(options|alternatives|suggest\w*|recommend\w*|compare|which (one|is best)|"
                           r"best \w+s|top \d+|show me \d+|\d+ (options|ideas|places|routes|ones))\b", re.I)

# Tools that change what's on screen: each is followed by a checkpoint in the trajectory.
CHANGES_DESKTOP = {"set_scene", "set_deck", "swap_scene", "guide_step", "insert_image_to_slides",
                   "blender_run", "open_terminal"}

PROMPTS = Path(__file__).resolve().parent / "prompts"
USER_NOTES = CONFIG.parent / "USER.md"
USER_TEMPLATE = """<!-- About you, for SceneBar: who you are, where you are, your preferences.
Sent to the model with every request: keep it short and keep secrets out.
/remember TEXT in the bar adds a line. Lines inside these comment marks are not sent. -->
"""


def user_notes():
    """USER.md without its comments; created with a short template the first time."""
    if not USER_NOTES.exists():
        USER_NOTES.parent.mkdir(parents=True, exist_ok=True)
        USER_NOTES.write_text(USER_TEMPLATE)
    return re.sub(r"<!--.*?-->", "", USER_NOTES.read_text(), flags=re.S).strip()


def remember(note):
    """Add a line to USER.md (from /remember)."""
    user_notes()
    with USER_NOTES.open("a") as f:
        f.write(f"- {note.strip()}\n")


def system_prompt(request="", apps=""):
    """The prompt layers for one request (see prompts/README.md): SYSTEM.md, ABOUT.md with
    the current settings, the user's USER.md, and notes for the apps that are on screen
    (`apps`: words from the scene captured at the start of the request) or named in it."""
    cfg, scene_cfg = load_config(), dscene_config.load()
    settings = (f"Settings now: brain {cfg['provider']} / {cfg['models'][cfg['provider']]}; "
                f"finding elements: {scene_cfg['grounding']['provider']}; "
                f"web answers open in: {scene_cfg['answer_engine']}.")
    parts = [(PROMPTS / "SYSTEM.md").read_text().strip(), (PROMPTS / "ABOUT.md").read_text().strip(),
             settings]
    notes = user_notes()
    if notes:
        parts.append("About the user (their own notes):\n" + notes)
    for note in sorted((PROMPTS / "apps").glob("*.md")):
        if note.stem in apps.lower() or note.stem in request.lower():
            parts.append(f"Notes for {note.stem}:\n" + note.read_text().strip())
    return "\n\n".join(parts)


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


def _stopped(name, text):
    """Did the user press Stop on a tutorial step? Then the run ends there, whatever the model
    would do next."""
    if name != "guide_step":
        return False
    try:
        return json.loads(text).get("answer") == "Stop"
    except ValueError:
        return False


def _opened_answer(calls, results):
    """The scene name if this step only opened a search or web page (the answer lane), else None."""
    if len(calls) != 1 or calls[0]["name"] != "set_scene":
        return None
    scene = (calls[0].get("arguments") or {}).get("scene") or {}
    panes = scene.get("panes") or []
    if not panes or any(p.get("kind") not in ("search", "url") for p in panes):
        return None
    if any(p.get("kind") == "url" for p in panes) and calls[0]["arguments"].get("look", True):
        return None  # the model wants to see the page: let it
    if results[0]["result"][0]["text"].startswith("ERROR"):
        return None
    return scene.get("name") or panes[0].get("query") or "search"


class Agent:
    """One conversation with the configured model, acting through dscene."""

    def __init__(self, on_event=print):
        self.on_event = on_event  # progress lines for the bar, e.g. "→ set_deck"
        self.reset()

    def reset(self):
        self.history = []        # Gemini conversation (Interactions steps), kept here
        self.messages = []       # OpenAI-compatible history, kept here
        self.pending = None      # a checkpoint being captured while the model thinks
        self.mode = "auto"       # or "teach" (/show) or "deck" (/deck), for the current request
        self.screens_set = 0
        self.scene_apps = ""     # what's on screen at the start of the request (app notes)

    def run(self, text, cfg=None, mode="auto"):
        """Handle one request; returns the model's final reply. Blocking. Recorded as a run.
        mode: "auto"; "teach" (/show: guide step by step, never change the app); "deck"
        (/deck: compare options as a deck; the only mode where the model may make decks)."""
        cfg = cfg or load_config()
        RECORDER.start(text, provider=cfg["provider"], model=cfg["models"][cfg["provider"]],
                       continues_conversation=bool(self.history or self.messages), mode=mode)
        self.mode = mode
        self.screens_set = 0  # successful set_scene calls in this request
        request = text
        if mode in MODES:
            text = f"{MODES[mode]}\n\n{text}"
        try:
            reply = asyncio.run(self._run(text, cfg))
        except Exception as e:
            while isinstance(e, BaseExceptionGroup) and e.exceptions:  # the MCP client wraps errors
                e = e.exceptions[0]
            RECORDER.end(error=f"{type(e).__name__}: {e}")
            raise BrainError(f"{type(e).__name__}: {e}") from e
        if mode == "auto" and WANTS_OPTIONS.search(request) and reply and "/deck" not in reply:
            reply = f"{reply} · /deck to compare options"
        RECORDER.end(reply=reply)
        return reply

    async def _run(self, text, cfg):
        from mcp import Client  # imported lazily: only the brain thread needs it

        async with Client(DSCENE_URL) as dscene:
            self.pending = None
            await self._checkpoint(dscene, "start")
            tools = [{"name": t.name, "description": t.description or "",
                      "parameters": plain_schema(t.input_schema)}
                     for t in (await dscene.list_tools()).tools
                     if self.mode == "deck" or t.name not in DECK_TOOLS]
            loop = self._gemini if cfg["provider"] == "gemini" else self._openai
            try:
                return await loop(cfg, text, tools, dscene)
            finally:
                await self._settle()
                await self._checkpoint(dscene, "end")

    async def _checkpoint(self, dscene, trigger, seq=None):
        """Record a restorable scene + thumbnail of the desktop; the model never sees it."""
        scene = jpeg = error = None
        try:
            scene = json.loads(_result_parts(await dscene.call_tool("capture_scene", {"name": trigger}))[0])
        except Exception as e:  # noqa: BLE001 - e.g. nothing visible to capture
            error = str(e)
        try:
            _, shots = _result_parts(await dscene.call_tool("get_screen", {}))
            jpeg = base64.b64decode(shots[0][1]) if shots else None
        except Exception:  # noqa: BLE001 - recording must never break a request
            pass
        RECORDER.checkpoint(trigger, scene, jpeg, error, seq)
        if trigger == "start":  # which apps are on screen, for the app notes in the prompt
            self.scene_apps = " ".join(f"{p.get('kind', '')} {p.get('app') or ''} {p.get('title') or ''}"
                                       for p in (scene or {}).get("panes", []))

    async def _settle(self):
        """Wait for a checkpoint still being captured, so it never mixes in the next op."""
        if self.pending:
            await asyncio.gather(self.pending, return_exceptions=True)
            self.pending = None

    async def _call(self, dscene, name, args):
        """Run one dscene tool; returns (text, images). Desktop changes get a checkpoint,
        captured while the model thinks about its next step."""
        await self._settle()
        if self.mode != "deck" and name in DECK_TOOLS:  # e.g. remembered from a /deck request
            RECORDER.op("agent", name, args=args, seconds=0, result="refused: not a /deck request")
            return "ERROR: decks are only for /deck requests. Open one search page instead " \
                   "(set_scene with a search pane).", []
        if self.mode != "deck" and name == "set_scene" and self.screens_set:
            RECORDER.op("agent", name, args=args, seconds=0, result="refused: one screen per request")
            return "ERROR: the screen is already set up for this request: reply now. Several " \
                   "options side by side are a deck, which the user asks for with /deck.", []
        if self.mode == "teach" and name in DOES_IT_FOR_USER and args.get("changes", True):
            RECORDER.op("agent", name, args=args, seconds=0, result="refused: teaching mode")
            return "ERROR: the user asked to be shown how (/show): don't do it for them. " \
                   "Guide them with guide_step, one step at a time.", []
        self.on_event(f"→ {name}")
        print(f"[agent] {name} {json.dumps(args)[:300]}", flush=True)
        t0 = time.perf_counter()
        try:
            text, images = _result_parts(await dscene.call_tool(name, args))
        except Exception as e:  # noqa: BLE001 - report tool failures back to the model
            text, images = f"ERROR: {e}", []
        seconds = round(time.perf_counter() - t0, 3)
        print(f"[agent]   = {text[:300]}{f' + {len(images)} image(s)' if images else ''} ({seconds}s)", flush=True)
        RECORDER.op("agent", name, args=args, seconds=seconds, result=text[:4000],
                    images=[RECORDER.image(data, mime) for mime, data in images])
        if name in CHANGES_DESKTOP and not text.startswith("ERROR"):
            self.pending = asyncio.create_task(self._checkpoint(dscene, f"after:{name}", RECORDER.reserve()))
            if name == "set_scene":
                self.screens_set += 1
        return text, images

    def _model_call(self, url, body, headers=None, step=0):
        """One request to the model, timed and recorded as a "think" op: what it decided."""
        t0 = time.perf_counter()
        resp = _http(url, body, headers)
        if "steps" in resp:  # Gemini
            steps = resp["steps"]
            calls = [s.get("name") for s in steps if s.get("type") == "function_call"]
            text = " ".join(c.get("text", "") for s in steps if s.get("type") == "model_output"
                            for c in s.get("content", []) if c.get("type") == "text")
            searched = any(s.get("type") == "google_search_call" for s in steps)
        else:  # OpenAI-compatible
            msg = resp["choices"][0]["message"]
            calls = [c["function"]["name"] for c in msg.get("tool_calls") or []]
            text, searched = msg.get("content") or "", False
        RECORDER.op("model", "think", step=step, seconds=round(time.perf_counter() - t0, 3),
                    usage=resp.get("usage"), calls=calls, searched=searched, text=text[:1000])
        return resp

    async def _gemini(self, cfg, text, tools, dscene):
        """Stateless: the conversation lives here and is sent whole with each call (store=false
        is ~1.8 s faster per call than keeping it on Google's side). Model steps go back exactly
        as received, as the API requires; only our own old screenshots are dropped."""
        key = gemini_key(cfg)
        if not key:
            raise BrainError("no Gemini API key: type /key gemini YOUR_KEY")
        declared = [{"type": "function", **t} for t in tools] + [{"type": "google_search"}]
        self._forget_old_images()
        self.history.append({"type": "user_input", "content": [{"type": "text", "text": text}]})
        system = await asyncio.to_thread(system_prompt, text, self.scene_apps)
        model = cfg["models"]["gemini"]
        level = THINKING_FALLBACK.get(model, cfg["thinking_level"])
        for step in range(cfg["max_steps"]):
            body = {"model": model, "input": self.history, "system_instruction": system,
                    "tools": declared, "generation_config": {"thinking_level": level}, "store": False}
            try:
                resp = await asyncio.to_thread(self._model_call, f"{GEMINI}/interactions", body,
                                               {"x-goog-api-key": key}, step)
            except BrainError as e:
                if "thinking" in str(e).lower() and level != "low":
                    level = body["generation_config"]["thinking_level"] = "low"  # model lacks this level
                    THINKING_FALLBACK[model] = "low"  # don't pay for the failed call again
                    RECORDER.op("model", "thinking_fallback", error=str(e))
                elif step == 0 and len(self.history) > 1 and "HTTP 400" in str(e):
                    self.history = body["input"] = self.history[-1:]  # history rejected: start fresh
                    RECORDER.op("model", "conversation_reset", error=str(e))
                else:
                    raise
                resp = await asyncio.to_thread(self._model_call, f"{GEMINI}/interactions", body,
                                               {"x-goog-api-key": key}, step)
            steps = resp.get("steps", [])
            self.history.extend(steps)
            calls = [s for s in steps if s.get("type") == "function_call"]
            if not calls:
                return "\n".join(c.get("text", "") for s in steps if s.get("type") == "model_output"
                                  for c in s.get("content", []) if c.get("type") == "text").strip()
            results = []
            for call in calls:
                text, images = await self._call(dscene, call["name"], call.get("arguments") or {})
                results.append({"type": "function_result", "name": call["name"], "call_id": call["id"],
                                "result": [{"type": "text", "text": text}] + [
                                    {"type": "image", "mime_type": mime, "data": data}
                                    for mime, data in images]})
            self.history.extend(results)
            if any(_stopped(r["name"], r["result"][0]["text"]) for r in results):
                return "Stopped."
            opened = _opened_answer(calls, results)
            if opened:  # the browser shows the answer: skip a model call just to say so
                return f"Opened: {opened}"
        raise BrainError(f"stopped after {cfg['max_steps']} steps")

    def _forget_old_images(self):
        """Screenshots from earlier requests: replaced by a note, so the history stays small."""
        for item in self.history:
            if item.get("type") == "function_result" and any(c.get("type") == "image" for c in item["result"]):
                item["result"] = [c for c in item["result"] if c.get("type") != "image"] + [
                    {"type": "text", "text": "(screenshot from an earlier request, omitted)"}]

    async def _openai(self, cfg, text, tools, dscene):
        declared = [{"type": "function", "function": t} for t in tools]
        system = {"role": "system", "content": await asyncio.to_thread(system_prompt, text, self.scene_apps)}
        if self.messages:
            self.messages[0] = system  # this request's prompt layers
        else:
            self.messages.append(system)
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
            stopped = False
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
                stopped = stopped or _stopped(call["function"]["name"], result)
            if stopped:  # every call has its tool message, so the history stays valid
                return "Stopped."
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
