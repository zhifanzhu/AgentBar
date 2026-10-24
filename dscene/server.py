"""dscene MCP server: the protocol agents use to control the agent bar's desktop.

Runs inside the bar process, in a background thread, serving MCP over HTTP at
http://127.0.0.1:8765/mcp. Tools that touch GTK are run on the GTK main loop;
slow work (Chrome, LibreOffice, the grounding model) runs in worker threads.

Tool tiers: observe (list_windows, get_state, get_screen, find_element),
present (prepare_scenes, show_scene, focus_window, restore_desktop, decks),
guide (annotate, clear_annotations, toast, guide_step), ask (ask_user,
select_region), act (insert_image_to_slides). There is deliberately no
click/type tool yet.
"""
import asyncio
import base64
import json
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Literal

from gi.repository import GLib
from mcp.server.mcpserver import Image, MCPServer
from pydantic import BaseModel, Field

import brain
import desktop
from dscene import spec

PORT = 8765
HERE = Path(__file__).resolve().parents[1]
INSTRUCTIONS = """dscene controls the user's Linux desktop (XFCE, one screen) through the agent bar.
A scene is a layout of 1-3 browser panes; a deck is a set of scenes the user flips through
with cards in the bar. Prefer scenes over describing things in text: for comparing options,
call prepare_scenes with one scene per option. Annotations draw on screen without changing
any app. Use ask_user when you need the user's choice or need them to act. Never invent URLs:
use a pane of kind "search" when you don't have a real one."""


class Target(BaseModel):
    """Where on screen to point. Give exactly one field."""
    pane: int | None = Field(None, description="Pane index of the scene on screen, 0 = leftmost")
    window: str | None = Field(None, description="Window id from list_windows")
    element: str | None = Field(
        None, description="A UI element described in words, e.g. \"the 'Add Modifier' button\"; "
                          "found visually by the grounding model")
    region: list[int] | None = Field(None, description="[x, y, width, height] in screen pixels")
    point: list[int] | None = Field(None, description="[x, y] in screen pixels")


async def _on_gtk(fn, *args):
    """Run fn(*args) on the GTK main loop and return its result."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def run():
        try:
            result = fn(*args)
            loop.call_soon_threadsafe(fut.set_result, result)
        except Exception as e:  # noqa: BLE001 - hand any failure back to the caller
            loop.call_soon_threadsafe(fut.set_exception, e)
        return False

    GLib.idle_add(run)
    return await fut


async def _on_gtk_later(fn, *args, timeout=None):
    """Run fn(*args, done) on the GTK main loop; wait until it calls done(result)."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def done(result):
        loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(result))

    def run():
        try:
            fn(*args, done)
        except Exception as e:  # noqa: BLE001
            loop.call_soon_threadsafe(fut.set_exception, e)
        return False

    GLib.idle_add(run)
    return await asyncio.wait_for(fut, timeout)


def _localize(png, description):
    """Ask the grounding model (Holo) where description is in the PNG screenshot."""
    cfg = brain.load_config()["grounding"]
    schema = {"type": "object", "properties": {"x": {"type": "integer", "minimum": 0, "maximum": 1000},
                                                "y": {"type": "integer", "minimum": 0, "maximum": 1000}},
              "required": ["x", "y"]}
    prompt = ("Localize an element on the GUI image according to the provided target and output a "
              f"click position.\n * You must output a valid JSON following the format: {json.dumps(schema)}\n"
              f" Your target is:\n{description}")
    body = {"model": cfg["model"], "temperature": 0.0, "max_tokens": 64,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}},
                {"type": "text", "text": prompt}]}],
            "structured_outputs": {"json": schema}, "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(f"{cfg['url']}/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        point = json.loads(json.load(r)["choices"][0]["message"]["content"])
    return point["x"], point["y"]


def build(bar):
    server = MCPServer("dscene", instructions=INSTRUCTIONS, log_level="WARNING")

    async def find(description):
        png, (w, h) = await _on_gtk(bar.screenshot_png)
        nx, ny = await asyncio.to_thread(_localize, png, description)
        return round(nx / 1000 * w), round(ny / 1000 * h)

    async def resolve(target):
        if target.region:
            x, y, w, h = target.region
            return (x, y, w, h)
        if target.point:
            x, y = target.point
            return (x - 18, y - 18, 36, 36)
        if target.pane is not None:
            rects = await _on_gtk(lambda: list(bar.desktop.rects))
            if not 0 <= target.pane < len(rects):
                raise ValueError(f"no pane {target.pane} on screen (showing {len(rects)} panes)")
            return rects[target.pane]
        if target.window:
            for win in await asyncio.to_thread(desktop.list_windows):
                if int(win["id"], 16) == int(target.window, 16):
                    return tuple(win["rect"])
            raise ValueError(f"no window {target.window}")
        if target.element:
            x, y = await find(target.element)
            return (x - 18, y - 18, 36, 36)
        raise ValueError("target needs one of pane, window, element, region, point")

    # -- observe ---------------------------------------------------------------
    @server.tool()
    async def list_windows() -> list[dict]:
        """List the desktop's windows: id, app, title, rect [x, y, w, h], minimized, and
        ours (true for the agent bar's own scene windows)."""
        return await asyncio.to_thread(desktop.list_windows)

    @server.tool()
    async def get_state() -> dict:
        """The agent bar's state: the deck (scene names and notes), which scene is showing
        (null = the user's own desktop), pane rects on screen, annotations, screen size."""
        return await _on_gtk(bar.dscene_state)

    @server.tool()
    async def get_screen(window: str | None = None) -> Image:
        """A screenshot (JPEG) of the whole screen, or of one window (id from list_windows).
        Use it to check what the user sees, e.g. whether a tutorial step really happened."""
        rect = None
        if window:
            rect = await resolve(Target(window=window))
        jpeg, _ = await _on_gtk(bar.screenshot_png, rect, "jpeg")
        return Image(data=jpeg, format="jpeg")

    @server.tool()
    async def find_element(description: str) -> dict:
        """Find a UI element on screen from a description (e.g. "the wrench icon in Blender's
        properties editor"). Returns its centre {x, y} in screen pixels. Uses a vision model;
        works in apps without accessibility data, like Blender."""
        x, y = await find(description)
        return {"x": x, "y": y}

    # -- present ---------------------------------------------------------------
    @server.tool()
    async def prepare_scenes(scenes: list[spec.SceneSpec], show_first: bool = True) -> dict:
        """Replace the deck with these scenes and show them as cards in the bar (the user flips
        with the arrow keys). Each scene is 1-3 browser panes side by side, e.g. an option's
        page next to a map. Page links that turn out not to exist are replaced by a web search.
        Returns the deck."""
        return await _on_gtk_later(bar.present_deck, scenes, show_first, timeout=120)

    @server.tool()
    async def show_scene(index: int) -> dict:
        """Switch the desktop to scene `index` of the current deck (0-based)."""
        return await _on_gtk(bar.show_scene, index)

    @server.tool()
    async def focus_window(window: str) -> dict:
        """Bring a window (id from list_windows) to the front, un-minimizing it. Use this to
        show the app a tutorial is about before annotating it."""
        await _on_gtk(bar.show_scene, None)  # scene windows would cover it
        await asyncio.to_thread(subprocess.run, ["wmctrl", "-i", "-a", window])
        await asyncio.sleep(0.5)  # let it repaint before anyone takes a screenshot
        return {"focused": window}

    @server.tool(structured_output=False)
    async def focus_app(name: str) -> list:
        """Bring an app to the front by name (matched against window app and title, e.g.
        "blender", "impress", "firefox") and return its window info plus a screenshot of the
        screen. One call instead of list_windows + focus_window + get_screen."""
        wanted = name.lower()
        windows = [w for w in await asyncio.to_thread(desktop.list_windows)
                   if not w["ours"] and (wanted in w["app"].lower() or wanted in w["title"].lower())]
        if not windows:
            raise ValueError(f"no window matches {name!r}; see list_windows")
        win = windows[0]
        await focus_window(win["id"])
        jpeg, _ = await _on_gtk(bar.screenshot_png, None, "jpeg")
        return [win, Image(data=jpeg, format="jpeg")]

    @server.tool()
    async def restore_desktop() -> dict:
        """Hide the scene windows and return to the user's own desktop."""
        return await _on_gtk(bar.show_scene, None)

    @server.tool()
    async def save_deck(name: str) -> dict:
        """Save the current deck under a name, so it can be reopened later with load_deck."""
        scenes = await _on_gtk(lambda: list(bar.desktop.scenes))
        if not scenes:
            raise ValueError("no deck to save")
        return {"saved": str(spec.save_deck(name, scenes)), "scenes": len(scenes)}

    @server.tool()
    async def load_deck(name: str, show_first: bool = True) -> dict:
        """Reopen a saved deck by name (see list_decks)."""
        _, scenes = spec.load_deck(name)
        return await _on_gtk_later(bar.present_deck, scenes, show_first, timeout=120)

    @server.tool()
    async def list_decks() -> list[str]:
        """Names of saved decks."""
        return spec.list_decks()

    # -- guide -----------------------------------------------------------------
    @server.tool()
    async def annotate(target: Target, text: str = "",
                       kind: Literal["box", "step", "label", "arrow", "spotlight"] = "box",
                       step: int | None = None) -> dict:
        """Draw on top of the screen without touching any app. kind: box (outline), step
        (numbered tutorial marker, give `step`), label (text only), arrow (arrow with text),
        spotlight (dim everything else). Returns the annotation id and the rect it marks."""
        rect = await resolve(target)
        item_id = await _on_gtk(bar.annotations.add, kind, rect, text, step)
        return {"id": item_id, "rect": list(rect)}

    @server.tool()
    async def clear_annotations(ids: list[int] | None = None) -> dict:
        """Remove annotations by id, or all of them when ids is omitted."""
        return {"removed": await _on_gtk(bar.annotations.remove, ids)}

    @server.tool()
    async def toast(text: str, seconds: int = 4) -> dict:
        """Show a short notice near the bottom of the screen."""
        await _on_gtk(bar.toast, GLib.markup_escape_text(text), seconds)
        return {"shown": True}

    # -- ask -------------------------------------------------------------------
    async def ask(question, options, timeout_s):
        try:
            return await _on_gtk_later(bar.ask, question, options, timeout=timeout_s)
        except TimeoutError:
            await _on_gtk(bar.ask_cancel)
            return None

    @server.tool()
    async def ask_user(question: str, options: list[str] | None = None, timeout_s: int = 300) -> dict:
        """Ask the user in the agent bar and wait for the answer. With options, they get one
        button per option; they can also type a free answer (F4 focuses the bar). The bar
        doesn't take keyboard focus, so the user's keys keep going to their app. Use it to
        confirm before anything risky or to let them choose. Returns {"answer": text}, or
        {"answer": null} if they don't answer in time."""
        return {"answer": await ask(question, options or [], timeout_s)}

    @server.tool(structured_output=False)
    async def guide_step(step: int, instruction: str, target: Target,
                         options: list[str] | None = None, timeout_s: int = 600) -> list:
        """One tutorial step in a single call: mark `target` with a numbered STEP badge and
        `instruction`, show the instruction in the bar (without taking keyboard focus from the
        user's app), wait until the user answers (default options "Done" / "Stop"), remove the
        mark, and return their answer plus a screenshot of the screen right afterwards, so you
        can check the step really happened before choosing the next one. A step is ONE click or
        key press; target an element by description (found visually) or by point/region."""
        rect = await resolve(target)
        item_id = await _on_gtk(bar.annotations.add, "step", rect, instruction, step)
        try:
            answer = await ask(f"Step {step}: {instruction}", options or ["Done", "Stop"], timeout_s)
        finally:
            await _on_gtk(bar.annotations.remove, [item_id])
        await asyncio.sleep(0.4)  # let the app repaint after the user's action
        jpeg, _ = await _on_gtk(bar.screenshot_png, None, "jpeg")
        return [{"step": step, "answer": answer, "marked": list(rect)}, Image(data=jpeg, format="jpeg")]

    @server.tool()
    async def select_region(prompt: str = "Drag to select") -> dict:
        """Let the user drag a box on a frozen copy of the screen. Returns the region
        [x, y, w, h] and the PNG path of the selected pixels, or region null if cancelled."""
        path, rect = await _on_gtk_later(bar.select_region, prompt, timeout=300)
        return {"region": list(rect) if rect else None, "path": path}

    # -- act -------------------------------------------------------------------
    @server.tool()
    async def insert_image_to_slides(path: str) -> dict:
        """Put an image (e.g. from select_region) on a new slide in LibreOffice Impress,
        starting Impress if needed. One Ctrl+Z in Impress undoes it."""
        r = await asyncio.to_thread(subprocess.run, [sys.executable, str(HERE / "impress.py"), path],
                                    capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError((r.stderr.strip().splitlines() or ["Impress failed"])[-1])
        info = json.loads(r.stdout)
        await asyncio.to_thread(subprocess.run, ["wmctrl", "-a", f"{info['title']} — LibreOffice Impress"])
        return info

    return server


def start(bar, host="127.0.0.1", port=PORT):
    server = build(bar)
    threading.Thread(target=lambda: asyncio.run(server.run_streamable_http_async(host=host, port=port)),
                     daemon=True, name="dscene").start()
    return f"http://{host}:{port}/mcp"
