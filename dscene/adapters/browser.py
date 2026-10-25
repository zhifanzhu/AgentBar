"""Browser panes: the agent's own Chromium windows ("slots"), driven over CDP.

Each slot is its own Chromium instance, so it has its own window that stays put while
its content changes. Over CDP we open, activate and close tabs, read and set the scroll
position, and screenshot tabs in the background (even in a minimized window).

The user browses in these windows too, so the agent only ever closes deck tabs it opened
itself (recorded in ~/.cache/dscene/agent-tabs.json, which survives a restart of the
bar) that the user hasn't taken up. Pages it opened for an ordinary request (answers,
videos, tutorials) are the user's to keep; see ChromeSlot.agent_tabs.
"""
import base64
import json
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

import websocket

from dscene.adapters.base import frame_rect, place_window, sh

AGENT_TABS = Path.home() / ".cache" / "dscene" / "agent-tabs.json"


def _agent_tabs():
    try:
        return json.loads(AGENT_TABS.read_text())
    except (OSError, ValueError):
        return {}


def _save_agent_tabs(tabs):
    AGENT_TABS.parent.mkdir(parents=True, exist_ok=True)
    AGENT_TABS.write_text(json.dumps(tabs))


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


NOT_FOUND = re.compile(r"\b404\b|not found|can.t be found|cannot be found|no longer exists", re.I)


def _same_place(a, b, slack=6):
    """Two window geometries match (allowing for a few pixels of window-manager rounding)."""
    if a["minimized"] or b["minimized"]:
        return a["minimized"] == b["minimized"]
    if a["maximized"] or b["maximized"]:
        return a["maximized"] == b["maximized"]
    return bool(a["rect"] and b["rect"]) and all(abs(x - y) <= slack for x, y in zip(a["rect"], b["rect"]))


def page_is_dead(port, target_id, wait=10.0):
    """After the tab has loaded: did the site say the page doesn't exist (HTTP 404/410, or
    a "page not found" title)? Checked in the real browser, which bot-blocking sites let
    through where a script's request gets a challenge page instead."""
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            if js(port, target_id, "document.readyState") != "loading":
                break
        except Exception:  # noqa: BLE001 - not ready to answer yet
            pass
        time.sleep(0.4)
    page = js(port, target_id, "({status: (performance.getEntriesByType('navigation')[0] || {})"
                                ".responseStatus, title: document.title})") or {}
    return page.get("status") in (404, 410) or bool(NOT_FOUND.search(page.get("title") or ""))


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
        self.wm_class = f"dscene-{role}"
        self.profile = Path.home() / ".cache" / "dscene" / role
        self.win_id = None
        self.rect = None  # last geometry we placed the window at
        self.shown_tab = None  # the tab this window last showed
        self.before = None  # its geometry before a deck took it over, to give back on /close
        self.placed = None  # its geometry where the deck last put it

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
        self.win_id = None  # gone (closed by the user); X may reuse its id for another window
        return False

    def pages(self):
        """Ordinary tabs (skips Chromium's own chrome:// UI pages)."""
        return [t for t in self._get("/json/list")
                if t.get("type") == "page" and not t.get("url", "").startswith("chrome://")]

    def open(self, url, deck=False, new_tab=False):
        """Show url in a tab of the agent's: its blank tab if it has one, else a new tab.
        deck: the tab is part of a deck, so /close may close it later. new_tab: always a new
        one (a blank tab may be about to be closed, or still loading the previous url)."""
        if url != "about:blank" and not new_tab:
            blank = next((t["id"] for t in self.pages() if t.get("url") in ("", "about:blank")), None)
            if blank:
                self.navigate(blank, url, deck)
                return blank
        target_id = self._get("/json/new?" + urllib.parse.quote(url, safe=""), method="PUT")["id"]
        tabs = _agent_tabs()
        tabs[target_id] = {"url": url, "history": 1, "deck": deck}
        _save_agent_tabs(tabs)
        return target_id

    def close(self, target_id):
        self._get(f"/json/close/{target_id}")
        tabs = _agent_tabs()
        if tabs.pop(target_id, None) is not None:
            _save_agent_tabs(tabs)

    def navigate(self, target_id, url, deck=None):
        """Load url in one of the agent's tabs. deck: whether it's now a deck tab (None:
        unchanged)."""
        try:
            before = js(self.port, target_id, "history.length") or 1
        except Exception:  # noqa: BLE001 - a tab that can't answer yet has just the one entry
            before = 1
        cdp(self.port, target_id, "Page.navigate", {"url": url})
        tabs = _agent_tabs()
        old = tabs.get(target_id)
        was_deck = isinstance(old, dict) and old.get("deck", False)
        tabs[target_id] = {"url": url, "history": before + 1, "deck": was_deck if deck is None else deck}
        _save_agent_tabs(tabs)

    def agent_tabs(self, replacing=False):
        """Ids of this window's tabs that are the agent's to close: blank tabs, and deck tabs
        the user hasn't taken up. Never: tabs the agent didn't open, pages it opened for an
        ordinary request (the user asked to see them), and deck tabs the user has moved on in
        (history grew: a click, a search, a video picked), took to another site, or that are
        playing sound or video. replacing: a new deck replaces this one (one deck at a time),
        so clicks within a deck page don't keep it; another site or playing media still do."""
        opened, mine = _agent_tabs(), []
        for t in self.pages():
            url = t.get("url", "")
            if url in ("", "about:blank"):
                mine.append(t["id"])
                continue
            record = opened.get(t["id"])
            if not isinstance(record, dict) or not record.get("deck"):
                continue  # not the agent's, or a page the user asked for
            if urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(record["url"]).netloc:
                continue
            try:
                used = js(self.port, t["id"], "({history: history.length, playing: [...document.querySelectorAll("
                                              "'video, audio')].some(m => !m.paused && !m.ended)})")
            except Exception:  # noqa: BLE001 - can't tell: leave it to the user
                continue
            if (replacing or used["history"] <= record["history"]) and not used["playing"]:
                mine.append(t["id"])
        return mine

    def visible_tabs(self, ids):
        """Those of ids that are on screen: the tab showing in a window that isn't minimized."""
        shown = []
        for target_id in ids:
            try:
                if tab_state(self.port, target_id)["visible"]:
                    shown.append(target_id)
            except Exception:  # noqa: BLE001
                continue
        return shown

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
        moved = (rect, maximized) != self.rect  # only move/resize when the layout changed
        if moved:
            place_window(self.win_id, rect, maximized, activate=False)
            self.rect = (rect, maximized)
        sh("wmctrl", "-i", "-a", self.win_id)
        if moved and self.before is not None:
            self.placed = self.geometry()  # where we put it; a user's move later isn't ours

    def hide(self):
        if self.find_window():  # never a stale id: it may now be another slot's window
            sh("xdotool", "windowminimize", str(int(self.win_id, 16)))
            self.rect = None  # the user may move it while it's ours to restore
            if self.before is not None:
                self.placed = self.geometry()

    def geometry(self):
        """Where the window is: {"rect": [x, y, w, h] with its frame, "maximized", "minimized"}."""
        state = sh("xprop", "-id", self.win_id, "_NET_WM_STATE")
        return {"rect": frame_rect(self.win_id),
                "maximized": "MAXIMIZED_VERT" in state and "MAXIMIZED_HORZ" in state,
                "minimized": "_NET_WM_STATE_HIDDEN" in state}

    def take_over(self):
        """A deck is about to use this window: remember where it was (once per deck run)."""
        if self.before is None and self.win_id:
            self.before = self.geometry()
            self.placed = None

    def give_back(self):
        """The deck is done with this window: put it back where it was, unless the user
        moved or resized it meanwhile (then where it is now is their choice)."""
        before, placed, self.before, self.placed = self.before, self.placed, None, None
        if not before or not self.win_id:
            return
        now = self.geometry()
        if placed is not None and not _same_place(now, placed):
            return  # the user moved it since the deck placed it
        if before["minimized"]:
            self.hide()
        else:
            place_window(self.win_id, before["rect"], before["maximized"], activate=False)
            self.rect = None
