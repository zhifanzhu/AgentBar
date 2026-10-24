"""LibreOffice Impress panes: document + slide, through the UNO worker (impress_uno.py).

The worker runs as a subprocess, so the bar never imports uno and a hung LibreOffice
connection can't freeze it. Unsaved or modified documents are captured as a copy;
restoring reuses the original if it's still open.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

from dscene.adapters.base import find_window, place_window
from dscene.spec import Pane

ROOT = Path(__file__).resolve().parents[2]
SUFFIX = " — LibreOffice Impress"


def run_uno(*args, timeout=120):
    """Run the UNO worker with args; returns its JSON reply."""
    r = subprocess.run([sys.executable, "-m", "dscene.adapters.impress_uno", *args],
                       capture_output=True, text=True, timeout=timeout, cwd=ROOT)
    if r.returncode != 0:
        raise RuntimeError((r.stderr.strip().splitlines() or ["Impress worker failed"])[-1])
    return json.loads(r.stdout)


def insert_image(path):
    """Put an image on a slide (see impress_uno.insert) and bring Impress to the front."""
    info = run_uno(str(path))
    subprocess.run(["wmctrl", "-a", f"{info['title']}{SUFFIX}"])
    return info


class ImpressAdapter:
    kind = "impress"

    def matches(self, win):
        return win["title"].endswith("LibreOffice Impress")

    def capture(self, win):
        state = run_uno("--capture")
        if not state.get("ok"):
            return Pane(kind="impress", rect=win["rect"], title=win["title"], fidelity="layout")
        return Pane(kind="impress", rect=win["rect"], title=win["title"], fidelity="exact",
                    doc=state["doc"], slide=state["slide"])

    def restore(self, pane, rect, windows_fn):
        if pane.doc:
            original = (pane.title or "").removesuffix(SUFFIX)
            title = run_uno("--restore", json.dumps({"doc": pane.doc, "slide": pane.slide,
                                                     "title": original}))["title"]
            time.sleep(0.5)
            win = find_window(windows_fn(), exact_title=f"{title}{SUFFIX}", title=title)
            fidelity = "exact"
        else:
            win = find_window(windows_fn(), title="LibreOffice Impress")
            fidelity = "layout"
        if not win:
            return {"kind": "impress", "ok": False, "note": "no Impress window"}
        place_window(win["id"], rect, pane.maximized)
        return {"kind": "impress", "window": win["id"], "ok": True, "fidelity": fidelity}

    def place(self, pane, rect, windows_fn):
        win = find_window(windows_fn(), exact_title=pane.title, title="LibreOffice Impress")
        if win:
            place_window(win["id"], rect, pane.maximized)
        return {"kind": "impress", "window": win and win["id"], "ok": bool(win), "fidelity": "layout"}
