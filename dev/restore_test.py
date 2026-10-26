#!/usr/bin/env python3
"""Round-trip test of scene restore over dscene (no LLM): capture A, change the screen,
set_scene(A), and compare. Run in the VM while the bar is up:
    python3 dev/restore_test.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dscene.client import URL, connect  # noqa: E402


def text(result):
    return [json.loads(c.text) for c in result.content if c.type == "text"]


async def main():
    async with connect(URL) as c:
        before = text(await c.call_tool("get_scene", {}))[0]["windows"]
        scene_a = text(await c.call_tool("capture_scene", {"name": "A"}))[0]
        print("captured A:")
        for p in scene_a["panes"]:
            extra = {k: p[k] for k in ("url", "scroll_y", "doc", "slide", "blend") if k in p}
            print(f"  {p['kind']:8s} {p['fidelity']:6s} {p['rect']} {p.get('title', '')[:35]!r} {extra}")

        # Change the screen: scroll the browser pane and put Blender full screen on top.
        url_panes = [p for p in scene_a["panes"] if p["kind"] == "url"]
        if url_panes:
            changed = dict(url_panes[0], scroll_y=0)
            changed.pop("rect"), changed.pop("fidelity")
            r = text(await c.call_tool("set_scene", {"scene": {"name": "scroll top", "panes": [changed]},
                                                     "look": False}))[0]
            print("set_scene scroll-top:", r["panes"])
        r = text(await c.call_tool("set_scene", {"scene": {"name": "Blender", "panes": [{"kind": "blender"}]},
                                                 "look": False}))[0]
        print("set_scene Blender full:", r["panes"])
        middle = text(await c.call_tool("get_scene", {}))[0]["windows"]

        # Bring A back.
        r = text(await c.call_tool("set_scene", {"scene": scene_a, "look": False}))[0]
        print("set_scene(A):", [(p["kind"], p.get("ok"), p.get("fidelity"), p.get("note")) for p in r["panes"]])
        await asyncio.sleep(1.0)
        after = text(await c.call_tool("get_scene", {}))[0]["windows"]
        scene_back = text(await c.call_tool("capture_scene", {"name": "A again"}))[0]

    def layout(ws):
        return {w["id"]: (tuple(w["rect"]), w["minimized"]) for w in ws}

    def stack(ws):
        return [w["id"] for w in ws if not w["minimized"]]

    moved = [i for i in layout(before) if layout(before)[i] != layout(after).get(i)]
    print("\nwindows changed in the middle:", sum(layout(before)[i] != layout(middle).get(i) for i in layout(before)))
    print("windows whose rect/minimized differ after restore:", moved or "none")
    for i in moved:
        print(f"   {i}: before {layout(before)[i]} after {layout(after).get(i)}")
    print("stacking order restored:", stack(before) == stack(after))
    a_urls = [(p["url"], p.get("scroll_y")) for p in scene_a["panes"] if p["kind"] == "url"]
    b_urls = [(p["url"], p.get("scroll_y")) for p in scene_back["panes"] if p["kind"] == "url"]
    print("browser url + scroll restored:", a_urls == b_urls, a_urls, b_urls)


asyncio.run(main())
