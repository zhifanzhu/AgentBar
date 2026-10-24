"""Browser panes: the agent's own Chromium windows ("slots"), driven over CDP.

Each slot is its own Chromium instance, so it has its own window that stays put while
its content changes. Over CDP we open, activate and close tabs, read and set the scroll
position, and screenshot tabs in the background (even in a minimized window).
"""
import base64
import json
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

import websocket

from dscene.adapters.base import place_window, sh


# -- CDP ---------------------------------------------------------------------------
def _ws_url(port, target_id):
    for t in json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5)):
        if t["id"] == target_id:
            return t["webSocketDebuggerUrl"]
    raise RuntimeError(f"no tab {target_id} on port {port}")


def cdp(port, target_id, method, params=None, timeout=10):
    ws = websocket.create_connection(_ws_url(port, target_id), timeout=timeout, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            reply = json.loads(ws.recv())
            if reply.get("id") == 1:
                if "error" in reply:
                    raise RuntimeError(reply["error"].get("message"))
                return reply["result"]
    finally:
        ws.close()


def js(port, target_id, expression):
    result = cdp(port, target_id, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
    return result["result"].get("value")


def tab_state(port, target_id):
    """{"url", "scroll_y", "visible"} of a tab."""
    url, y, visibility = js(port, target_id, "[location.href, Math.round(scrollY), document.visibilityState]")
    return {"url": url, "scroll_y": y, "visible": visibility == "visible"}


def set_scroll(port, target_id, y, wait=8.0):
    """Scroll once the page has loaded (pages restore at the top, then grow)."""
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            if js(port, target_id, "document.readyState") == "complete":
                break
        except Exception:  # noqa: BLE001 - the page may still be navigating
            pass
        time.sleep(0.2)
    js(port, target_id, f"window.scrollTo(0, {int(y)}); Math.round(scrollY)")


def tab_screenshot(port, target_id, attempts=3):
    """JPEG of a tab, even in a background tab of a minimized window (fromSurface=false).
    Right after a window is minimized Chromium can stall a capture, so retry briefly."""
    for attempt in range(attempts):
        try:
            data = cdp(port, target_id, "Page.captureScreenshot",
                       {"format": "jpeg", "quality": 70, "fromSurface": False}, timeout=6)["data"]
            return base64.b64decode(data)
        except (websocket.WebSocketTimeoutException, TimeoutError):
            if attempt == attempts - 1:
                raise
            time.sleep(0.8)


# -- slots -------------------------------------------------------------------------
class ChromeSlot:
    """One agent-owned Chromium window, reachable over CDP on its own port."""

    def __init__(self, role, port):
        self.role = role
        self.port = port
        self.wm_class = f"agentbar-{role}"
        self.profile = Path.home() / ".cache" / "agentbar" / role
        self.win_id = None
        self.rect = None  # last geometry we placed the window at
        self.shown_tab = None  # the tab this window last showed

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
        self.shown_tab = target_id

    def current_tab(self):
        """The tab the user sees in this window, or (when it's hidden) the one it last showed."""
        pages = [t["id"] for t in self.pages()]
        for target_id in pages:
            try:
                if tab_state(self.port, target_id)["visible"]:
                    return target_id
            except Exception:  # noqa: BLE001 - a tab still loading may not answer
                continue
        return self.shown_tab if self.shown_tab in pages else (pages[0] if pages else None)

    def show_url(self, url):
        """Activate a tab showing url (reusing an open one); returns its target id."""
        for t in self.pages():
            if t.get("url") == url:
                self.activate(t["id"])
                return t["id"]
        target_id = self.open(url)
        self.activate(target_id)
        return target_id

    def visible_tab_state(self):
        """URL and scroll of the tab the user is looking at in this window."""
        for t in self.pages():
            try:
                state = tab_state(self.port, t["id"])
            except Exception:  # noqa: BLE001 - tabs still loading may not answer
                continue
            if state["visible"]:
                return state
        return None

    def show(self, rect, maximized=False):
        if (rect, maximized) != self.rect:  # only move/resize when the layout changed
            place_window(self.win_id, rect, maximized, activate=False)
            self.rect = (rect, maximized)
        sh("wmctrl", "-i", "-a", self.win_id)

    def hide(self):
        if self.win_id:
            sh("xdotool", "windowminimize", str(int(self.win_id, 16)))
            self.rect = None  # the user may move it while it's ours to restore
