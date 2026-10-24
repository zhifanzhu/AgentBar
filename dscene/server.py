"""dscene MCP server: the protocol agents use to control the agent bar's desktop.

Runs inside the bar process, in a background thread, serving MCP over HTTP at
http://127.0.0.1:8765/mcp. Tools that touch GTK are run on the GTK main loop;
slow work (Chrome, Blender, LibreOffice, the grounding model) runs in worker threads.

Core tools (14):
  scene  get_scene, set_scene, capture_scene, set_deck, swap_scene, save_deck
  guide  annotate (incl. toasts), clear_annotations, guide_step
  look   get_screen, find_element
  user   ask_user, select_region
  app    insert_image_to_slides
There is deliberately no click/type tool: app state is set through app APIs, and the
user does the clicking in tutorials.
"""
import asyncio
import base64
import json
import threading
import urllib.request
from typing import Literal

from gi.repository import GLib
from mcp.server.mcpserver import Image, MCPServer
from pydantic import BaseModel, Field

import brain
from dscene import desktop, spec
from dscene.adapters import browser, impress

PORT = 8765
INSTRUCTIONS = """dscene controls the user's Linux desktop (XFCE, one screen) through the agent bar.
A scene says what each visible window shows and where: browser panes (url, map, search) and
the user's app windows (blender, impress, other). set_scene makes the screen show a scene,
restoring each window's state; capture_scene describes the current screen as a scene that
set_scene can bring back. A deck is a set of browser scenes the user flips through with
cards in the bar (set_deck, swap_scene). Annotations draw on screen without changing apps.
Never invent URLs: use a pane of kind "search" when you don't have a real one."""


class Target(BaseModel):
    """Where on screen to point. Give exactly one field."""
    pane: int | None = Field(None, description="Pane index of the scene on screen, 0 = first")
    window: str | None = Field(None, description="Window id from get_scene")
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

    async def screen_jpeg():
        jpeg, _ = await _on_gtk(bar.screenshot_png, None, "jpeg")
        return Image(data=jpeg, format="jpeg")

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

    async def ask(question, options, timeout_s):
        try:
            return await _on_gtk_later(bar.ask, question, options, timeout=timeout_s)
        except TimeoutError:
            await _on_gtk(bar.ask_cancel)
            return None

    # -- scene -----------------------------------------------------------------
    @server.tool()
    async def get_scene() -> dict:
        """What's on screen now, cheaply: the windows on the current workspace (id, app, title,
        rect, minimized; bottom of the stack first), the deck and which card is showing, pane
        rects, annotations, any open question, and saved decks. Use capture_scene for full,
        restorable state."""
        state = await _on_gtk(bar.dscene_state)
        windows = await asyncio.to_thread(desktop.list_windows, True)
        state["windows"] = [w for w in windows if not w["ours"] or w["slot"]]
        state["saved_decks"] = spec.list_decks()
        return state

    @server.tool(structured_output=False)
    async def set_scene(scene: spec.SceneSpec | None = None, look: bool = True) -> list:
        """Make the screen show `scene`: reuse matching windows, restore each pane's state
        (page and scroll, Blender checkpoint/selection/view, Impress document and slide),
        place the panes (later panes on top). E.g. {"name": "Blender", "panes": [{"kind":
        "blender"}]} just brings Blender to the front, full screen. A scene from capture_scene
        brings that moment back. Omit `scene` to hide the agent's panes and return to the
        user's own windows. Returns a report per pane, plus a screenshot when `look`."""
        if scene is None:
            result = await _on_gtk(bar.show_scene, None)
        else:
            result = await _on_gtk_later(bar.apply_scene, scene, timeout=180)
        await asyncio.sleep(0.4)  # let windows repaint before the screenshot
        return [result, await screen_jpeg()] if look else [result]

    @server.tool()
    async def capture_scene(name: str = "captured") -> dict:
        """Describe the current workspace as a scene: every visible window with its state
        descriptor (exact for Blender and Impress, page and scroll for browser panes, position
        only for other apps). Pass it to set_scene later to bring this moment back. Blender
        and Impress states are saved as checkpoint files."""
        scene = await asyncio.to_thread(bar.desktop.capture_scene, name)
        if scene is None:
            return {"name": name, "panes": [], "note": "empty desktop: no visible windows"}
        return scene.model_dump(exclude_none=True)

    @server.tool()
    async def set_deck(scenes: list[spec.SceneSpec] | None = None, saved: str | None = None,
                       show_first: bool = True) -> dict:
        """Replace the deck with these scenes (or a saved deck by name) and show them as cards
        in the bar; the user flips with the arrow keys. Use it to compare options: one scene
        per option, e.g. its page next to a map. Browser panes are preloaded so flipping is
        instant. Page links that don't exist are replaced by a web search."""
        if saved:
            _, scenes = spec.load_deck(saved)
        if not scenes:
            raise ValueError("give scenes or the name of a saved deck")
        return await _on_gtk_later(bar.present_deck, scenes, show_first, timeout=120)

    @server.tool()
    async def swap_scene(index: int | None) -> dict:
        """Show deck scene `index` (0-based), or null for the user's own windows. Instant."""
        return await _on_gtk(bar.show_scene, index)

    @server.tool()
    async def save_deck(name: str) -> dict:
        """Save the current deck under a name, to reopen later with set_deck(saved=name)."""
        scenes = await _on_gtk(lambda: list(bar.desktop.scenes))
        if not scenes:
            raise ValueError("no deck to save")
        return {"saved": str(spec.save_deck(name, scenes)), "scenes": len(scenes)}

    # -- guide -----------------------------------------------------------------
    @server.tool()
    async def annotate(text: str = "", target: Target | None = None,
                       kind: Literal["box", "step", "label", "arrow", "spotlight", "toast"] = "box",
                       step: int | None = None, seconds: int = 4) -> dict:
        """Draw on top of the screen without touching any app. kind: box (outline), step
        (numbered tutorial marker, give `step`), label (text only), arrow (arrow with text),
        spotlight (dim everything else), toast (a short notice at the bottom for `seconds`; no
        target). Returns the annotation id and the rect it marks."""
        if kind == "toast":
            await _on_gtk(bar.toast, GLib.markup_escape_text(text), seconds)
            return {"shown": True}
        if target is None:
            raise ValueError(f"kind {kind} needs a target")
        rect = await resolve(target)
        item_id = await _on_gtk(bar.annotations.add, kind, rect, text, step)
        return {"id": item_id, "rect": list(rect)}

    @server.tool()
    async def clear_annotations(ids: list[int] | None = None) -> dict:
        """Remove annotations by id, or all of them when ids is omitted."""
        return {"removed": await _on_gtk(bar.annotations.remove, ids)}

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
        return [{"step": step, "answer": answer, "marked": list(rect)}, await screen_jpeg()]

    # -- look ------------------------------------------------------------------
    @server.tool()
    async def get_screen(window: str | None = None) -> Image:
        """A screenshot (JPEG) of the whole screen, or of one window by id. A window is captured
        even when covered by others; the agent's browser panes even when hidden or minimized."""
        if window is None:
            return await screen_jpeg()
        wins = {int(w["id"], 16): w for w in await asyncio.to_thread(desktop.list_windows)}
        win = wins.get(int(window, 16))
        if win is None:
            raise ValueError(f"no window {window}")
        if win["slot"]:  # a browser pane: ask Chromium directly, works while hidden
            slot = next(s for s in bar.desktop.slots if s.role == win["slot"])
            tab = await asyncio.to_thread(slot.current_tab)
            return Image(data=await asyncio.to_thread(browser.tab_screenshot, slot.port, tab), format="jpeg")
        jpeg = await _on_gtk(bar.screenshot_window, window)
        return Image(data=jpeg, format="jpeg")

    @server.tool()
    async def find_element(description: str) -> dict:
        """Find a UI element on screen from a description (e.g. "the wrench icon in Blender's
        properties editor"). Returns its centre {x, y} in screen pixels. Uses a vision model;
        works in apps without accessibility data, like Blender."""
        x, y = await find(description)
        return {"x": x, "y": y}

    # -- user ------------------------------------------------------------------
    @server.tool()
    async def ask_user(question: str, options: list[str] | None = None, timeout_s: int = 300) -> dict:
        """Ask the user in the agent bar and wait for the answer. With options, they get one
        button per option; they can also type a free answer (F4 focuses the bar). The bar
        doesn't take keyboard focus, so the user's keys keep going to their app. Returns
        {"answer": text}, or {"answer": null} if they don't answer in time."""
        return {"answer": await ask(question, options or [], timeout_s)}

    @server.tool()
    async def select_region(prompt: str = "Drag to select") -> dict:
        """Let the user drag a box on a frozen copy of the screen. Returns the region
        [x, y, w, h] and the PNG path of the selected pixels, or region null if cancelled."""
        path, rect = await _on_gtk_later(bar.select_region, prompt, timeout=300)
        return {"region": list(rect) if rect else None, "path": path}

    # -- app -------------------------------------------------------------------
    @server.tool()
    async def insert_image_to_slides(path: str) -> dict:
        """Put an image (e.g. from select_region) on a new slide in LibreOffice Impress,
        starting Impress if needed. One Ctrl+Z in Impress undoes it."""
        return await asyncio.to_thread(impress.insert_image, path)

    return server


def start(bar, host="127.0.0.1", port=PORT):
    server = build(bar)
    threading.Thread(target=lambda: asyncio.run(server.run_streamable_http_async(host=host, port=port)),
                     daemon=True, name="dscene").start()
    return f"http://{host}:{port}/mcp"
