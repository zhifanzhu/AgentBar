"""Desktop control: Chrome windows driven over CDP, placed with wmctrl.

Each pane slot (up to three) is its own Chromium instance, so it has its own
window that stays put while its content changes. A scene is then just
"activate these tabs in these slots, at these positions".
"""
import json
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from dscene.spec import layout_rects

MAX_PANES = 3
FIRST_PORT = 9230


def sh(*args):
    return subprocess.run(args, capture_output=True, text=True).stdout


class ChromeRole:
    def __init__(self, role, port):
        self.role = role
        self.port = port
        self.wm_class = f"agentbar-{role}"
        self.profile = Path.home() / ".cache" / "agentbar" / role
        self.win_id = None
        self.rect = None  # last geometry we placed the window at

    def _get(self, path, method="GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read().decode()
        try:
            return json.loads(body)
        except ValueError:
            return body  # e.g. "Target activated"

    def alive(self):
        try:
            self._get("/json/version")
            return True
        except OSError:
            return False

    def start(self):
        if not self.alive():
            subprocess.Popen(
                ["chromium", f"--user-data-dir={self.profile}",
                 f"--remote-debugging-port={self.port}", f"--class={self.wm_class}",
                 "--no-first-run", "--no-default-browser-check",
                 "--password-store=basic", "--new-window", "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.alive() and self.find_window():
                return
            time.sleep(0.2)
        raise RuntimeError(f"{self.role}: Chromium did not come up")

    def find_window(self):
        # WM_CLASS is "res_name.res_class"; Chromium's res_name contains spaces.
        for line in sh("wmctrl", "-lx").splitlines():
            if f".{self.wm_class} " in line:
                self.win_id = line.split()[0]
                return True
        return False

    def pages(self):
        """Ordinary tabs (skips Chromium's own chrome:// UI pages)."""
        return [t for t in self._get("/json/list")
                if t.get("type") == "page" and not t.get("url", "").startswith("chrome://")]

    def open(self, url):
        return self._get("/json/new?" + urllib.parse.quote(url, safe=""), method="PUT")["id"]

    def close(self, target_id):
        self._get(f"/json/close/{target_id}")

    def activate(self, target_id):
        self._get(f"/json/activate/{target_id}")

    def show(self, rect):
        if rect != self.rect:  # only move/resize when the layout changed
            x, y, w, h = rect
            sh("wmctrl", "-i", "-r", self.win_id, "-b", "remove,maximized_vert,maximized_horz")
            sh("wmctrl", "-i", "-r", self.win_id, "-e", f"0,{x},{y},{w},{h}")
            self.rect = rect
        sh("wmctrl", "-i", "-a", self.win_id)

    def hide(self):
        if self.win_id:
            sh("xdotool", "windowminimize", str(int(self.win_id, 16)))
            self.rect = None  # the user may move it while it's ours to restore


class Desktop:
    def __init__(self):
        self.slots = [ChromeRole(f"pane{i}", FIRST_PORT + i) for i in range(MAX_PANES)]
        self.scenes = []         # SceneSpec per deck entry
        self.tabs = []           # per scene: one target id per pane
        self.rects = []          # where the current scene's panes are on screen
        self.saved_focus = None  # the user's window from before the bar was summoned

    def prepare(self, scenes):
        """Open every scene's tabs up front, so switching is only tab activation."""
        used = self.slots[:max(len(s.panes) for s in scenes)]
        for slot in used:
            slot.start()
        stale = {slot.role: [t["id"] for t in slot.pages()] for slot in used}
        self.tabs = [[used[j].open(url) for j, url in enumerate(s.urls())] for s in scenes]
        self.scenes = scenes
        for slot in self.slots:
            for target_id in stale.get(slot.role, []):
                slot.close(target_id)
            slot.hide()

    def apply(self, i, area):
        """Show scene i inside area = (x, y, w, h). Returns the pane rects."""
        scene, tabs = self.scenes[i], self.tabs[i]
        self.rects = layout_rects(scene.layout, area)
        for slot, tab in zip(self.slots, tabs):
            slot.activate(tab)
        for j, slot in enumerate(self.slots):
            if j < len(tabs):
                slot.show(self.rects[j])
            else:
                slot.hide()
        return self.rects

    def restore(self):
        """Back to the user's own desktop: hide the scene windows."""
        for slot in self.slots:
            slot.hide()
        self.rects = []

    def remember_focus(self):
        """Note the user's focused window (unless it's one of ours) to return to later."""
        active = sh("xdotool", "getactivewindow").strip()
        # By WM_CLASS, not id: scene windows can outlive a restart of the bar.
        if active and not sh("xdotool", "getwindowclassname", active).lower().startswith("agentbar"):
            self.saved_focus = active

    def refocus(self):
        if self.saved_focus:
            sh("xdotool", "windowactivate", self.saved_focus)


def list_windows():
    """Normal top-level windows: id, app class, title, geometry, minimized, ours."""
    out = []
    for line in sh("wmctrl", "-lxG").splitlines():
        parts = line.split(None, 8)
        if len(parts) < 8 or parts[1] == "-1":  # -1 = desktop, panel and other sticky chrome
            continue
        win_id, _, x, y, w, h, wm_class = parts[:7]
        title = parts[8] if len(parts) > 8 else ""
        state = sh("xprop", "-id", win_id, "_NET_WM_STATE")
        out.append({"id": win_id, "app": wm_class.split(".")[-1], "title": title,
                    "rect": [int(x), int(y), int(w), int(h)],
                    "minimized": "_NET_WM_STATE_HIDDEN" in state,
                    "ours": wm_class.lower().split(".")[-1].startswith("agentbar")})
    return out
