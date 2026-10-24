"""Blender panes, through the bridge that runs inside Blender (blender_bridge.py).

capture saves a .blend checkpoint plus selection, mode, 3D view and properties tab;
restore opens a checkpoint (after saving the current state) and re-applies the rest.
Without the bridge (a Blender started before it was installed), Blender is placed only.
"""
import json
import socket
import subprocess
import time

from dscene.adapters.base import find_window, place_window
from dscene.spec import Pane

BRIDGE_PORT = 8766
STATE = ("blend", "selected", "active", "mode", "view", "properties_tab")


def call(request, timeout=60):
    with socket.create_connection(("127.0.0.1", BRIDGE_PORT), timeout=timeout) as s:
        s.sendall((json.dumps(request) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    reply = json.loads(data)
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error", "Blender bridge error"))
    return reply


def bridge_up():
    try:
        call({"cmd": "ping"}, timeout=2)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


class BlenderAdapter:
    kind = "blender"

    def matches(self, win):
        return win["app"].lower() == "blender"

    def capture(self, win):
        if not bridge_up():
            return Pane(kind="blender", rect=win["rect"], title=win["title"], fidelity="layout")
        state = call({"cmd": "capture"})
        return Pane(kind="blender", rect=win["rect"], title=win["title"], fidelity="exact",
                    **{k: state.get(k) for k in STATE})

    def _window(self, windows_fn, launch_file=None):
        win = find_window(windows_fn(), app="blender")
        if win or launch_file is None:
            return win
        subprocess.Popen(["blender", launch_file] if launch_file else ["blender"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(100):  # Blender takes a few seconds to open its window and bridge
            time.sleep(0.3)
            win = find_window(windows_fn(), app="blender")
            if win and bridge_up():
                return win
        return win

    def restore(self, pane, rect, windows_fn):
        wants_state = any(getattr(pane, k) is not None for k in STATE)
        win = self._window(windows_fn, launch_file=pane.blend or "")
        if not win:
            return {"kind": "blender", "ok": False, "note": "Blender isn't open and couldn't be started"}
        report = {"kind": "blender", "window": win["id"], "ok": True, "fidelity": "layout"}
        if wants_state:
            if bridge_up():
                reply = call({"cmd": "restore", "state": {k: getattr(pane, k) for k in STATE}})
                report["fidelity"] = "exact"
                if reply.get("previous"):
                    report["previous_checkpoint"] = reply["previous"]["blend"]
            else:
                report["note"] = ("state not restored: this Blender was started before the dscene "
                                  "bridge was installed; restart Blender to enable it")
        place_window(win["id"], rect, pane.maximized)
        return report

    def place(self, pane, rect, windows_fn):
        win = find_window(windows_fn(), app="blender")
        if win:
            place_window(win["id"], rect, pane.maximized)
        return {"kind": "blender", "window": win and win["id"], "ok": bool(win), "fidelity": "layout"}
