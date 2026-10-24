"""Scene reconciliation: layouts, decks, set_scene and capture_scene.

Browser panes go into the agent's Chromium slots (adapters/browser.py); app panes are
the user's own windows, restored by their adapters (adapters/blender.py, impress.py,
window.py). A deck preloads its browser tabs, so swapping scenes is only tab activation.
"""
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from dscene.adapters import APP_ADAPTERS, adapter_for
from dscene.adapters.base import frame_rect, sh
from dscene.adapters.browser import ChromeSlot, page_is_dead, set_scroll
from dscene.spec import BROWSER_KINDS, Pane, SceneSpec, layout_rects, search_url

MAX_PANES = 3
FIRST_PORT = 9230


class Desktop:
    def __init__(self):
        self.slots = [ChromeSlot(f"pane{i}", FIRST_PORT + i) for i in range(MAX_PANES)]
        self.scenes = []         # SceneSpec per deck entry
        self.tabs = []           # per deck scene: one target id per browser pane
        self.rects = []          # where the current scene's panes are on screen
        self.saved_focus = None  # the user's window from before the bar was summoned

    # -- decks: browser panes preloaded, so swapping is only tab activation ------
    def prepare(self, scenes):
        """Open every scene's browser tabs up front, so switching is only tab activation."""
        used = self.slots[:max(len(s.browser_panes()) for s in scenes)]
        if len(used) > MAX_PANES:
            raise ValueError(f"at most {MAX_PANES} browser panes per scene")
        for slot in used:
            if slot.alive():
                slot.find_window()
                slot.take_over()  # an existing window: remember where it was, for /close
            slot.start()
        old = {t for tabs in self.tabs for t in tabs}  # the previous deck's tabs, if still the agent's
        stale = {slot.role: [t for t in slot.agent_tabs() if t in old] for slot in used}
        self.tabs = [[used[j].open(url) for j, url in enumerate(s.urls())] for s in scenes]
        self.scenes = scenes
        for slot in self.slots:
            for target_id in stale.get(slot.role, []):
                slot.close(target_id)
            slot.hide()

    def fix_dead_links(self):
        """Once the deck's tabs have loaded: a page link the site says doesn't exist (often a
        URL the model half-remembered) becomes a search for the scene on that same site,
        which usually finds the real page first. Returns the indexes of scenes changed."""
        scenes, tabs = self.scenes, self.tabs
        checks = []  # (scene index, pane index, slot, tab)
        for i, scene in enumerate(scenes):
            browser = [j for j, p in enumerate(scene.panes) if p.kind in BROWSER_KINDS]
            for b, (j, tab) in enumerate(zip(browser, tabs[i])):
                if scene.panes[j].kind == "url":
                    checks.append((i, j, self.slots[b], tab))
        with ThreadPoolExecutor(8) as pool:
            dead = list(pool.map(lambda c: page_is_dead(c[2].port, c[3]), checks))
        changed = set()
        for (i, j, slot, tab), is_dead in zip(checks, dead):
            if not is_dead or self.scenes is not scenes:  # the deck was replaced meanwhile
                continue
            site = urllib.parse.urlsplit(scenes[i].panes[j].url).netloc.removeprefix("www.")
            query = f"{scenes[i].name} site:{site}"
            slot.navigate(tab, search_url(query))
            scenes[i].panes[j] = Pane(kind="search", query=query)
            scenes[i].note = (scenes[i].note + " · page gone, showing a site search").lstrip(" ·")
            changed.add(i)
        return sorted(changed)

    def apply(self, i, area):
        """Show deck scene i inside area = (x, y, w, h). App panes are placed, not restored."""
        scene, tabs = self.scenes[i], self.tabs[i]
        self.rects = layout_rects(scene, area)
        for slot, tab in zip(self.slots, tabs):
            slot.activate(tab)
        browser = 0
        for pane, rect in zip(scene.panes, self.rects):
            if pane.kind in BROWSER_KINDS:
                self.slots[browser].show(rect)
                browser += 1
            else:
                APP_ADAPTERS[pane.kind].place(pane, rect, list_windows)
        for slot in self.slots[browser:]:
            slot.hide()
        return self.rects

    # -- single scenes: reconcile the desktop towards a spec (slow; run off the GTK thread) --
    def set_scene(self, scene, area):
        """Make the screen show scene: reuse windows, restore each pane's state, place it.
        Later panes end up on top. Returns one report per pane."""
        rects = layout_rects(scene, area)
        if len(scene.browser_panes()) > MAX_PANES:
            raise ValueError(f"at most {MAX_PANES} browser panes per scene")
        reports, browser = [], 0
        for pane, rect in zip(scene.panes, rects):
            if pane.kind in BROWSER_KINDS:
                slot = self.slots[browser]
                browser += 1
                slot.start()
                tab = slot.show_url(pane.resolve())
                if pane.scroll_y:
                    set_scroll(slot.port, tab, pane.scroll_y)
                slot.show(rect, pane.maximized)
                reports.append({"kind": pane.kind, "window": slot.win_id, "ok": True, "fidelity": "view"})
            else:
                try:
                    reports.append(APP_ADAPTERS[pane.kind].restore(pane, rect, list_windows))
                except Exception as e:  # noqa: BLE001 - one pane failing shouldn't stop the rest
                    reports.append({"kind": pane.kind, "ok": False, "note": f"{type(e).__name__}: {e}"})
        for slot in self.slots[browser:]:
            slot.hide()
        self.rects = list(rects)
        return reports

    def capture_scene(self, name="captured"):
        """The current workspace as a free-layout SceneSpec, bottom window first."""
        panes = []
        for win in list_windows(current_workspace=True):
            if win["minimized"] or (win["ours"] and not win["slot"]):
                continue  # the bar and other agent chrome aren't part of the user's scene
            if win["slot"]:
                slot = next(s for s in self.slots if s.role == win["slot"])
                state = slot.visible_tab_state()
                if state:
                    panes.append(Pane(kind="url", url=state["url"], scroll_y=state["scroll_y"],
                                      rect=win["rect"], maximized=win["maximized"] or None,
                                      title=win["title"], fidelity="view"))
                continue
            try:
                pane = adapter_for(win).capture(win)
            except Exception:  # noqa: BLE001 - fall back to the window's place on screen
                pane = APP_ADAPTERS["window"].capture(win)
            pane.maximized = win["maximized"] or None
            panes.append(pane)
        return SceneSpec(name=name, layout="free", panes=panes) if panes else None  # None: empty desktop

    def close_deck(self, keep=None):
        """Drop the deck and the other tabs the agent opened (answers, decks from before a
        restart). The user's own tabs always stay. keep: a deck scene index whose tabs stay
        open where they are (the card the user is looking at; a later close_deck closes them).
        A window left with only blank tabs is hidden (Chromium keeps running, so the next deck
        opens quickly); one with the user's tabs shows their last tab."""
        kept = set(self.tabs[keep]) if keep is not None and keep < len(self.tabs) else set()
        for slot in self.slots:
            if not slot.alive():
                continue
            mine = [t for t in slot.agent_tabs() if t not in kept]
            left = [t["id"] for t in slot.pages() if t["id"] not in mine]  # the user's, and kept
            if not left:
                slot.open("about:blank")  # closing a window's last tab would close the window
            for target_id in mine:
                slot.close(target_id)
            if kept & set(left):
                continue  # this window shows the kept scene: leave it (a later close finishes it)
            if left:
                slot.activate(left[-1])  # back to the user's own tab...
                slot.give_back()         # ...in the window where it was before the deck
            else:
                slot.hide()
                slot.before = slot.placed = None
        self.scenes, self.tabs, self.rects = [], [], []

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


def list_windows(current_workspace=False):
    """Normal top-level windows, bottom of the stack first: id, app class, title, rect,
    workspace, minimized, ours (the bar's own windows), slot (which pane slot, if any)."""
    stacking = sh("xprop", "-root", "_NET_CLIENT_LIST_STACKING").split("#")[-1]
    order = {int(w, 16): i for i, w in enumerate(stacking.replace(",", " ").split())}
    here = sh("xdotool", "get_desktop").strip()
    host = sh("uname", "-n").strip()
    out = []
    for line in sh("wmctrl", "-lxG").splitlines():
        # "id workspace x y w h res_name.res_class host title"; Chromium's res_name has spaces,
        # so split around the host name rather than on whitespace.
        head, _, title = line.partition(f" {host} ")
        parts = head.split(None, 6)
        if len(parts) < 7 or parts[1] == "-1":  # -1 = desktop, panel and other sticky chrome
            continue
        win_id, workspace, x, y, w, h, wm_class = parts
        if current_workspace and workspace != here:
            continue
        res_class = wm_class.strip().split(".")[-1]  # wmctrl pads the column
        state = sh("xprop", "-id", win_id, "_NET_WM_STATE")
        rect = frame_rect(win_id) or [int(x), int(y), int(w), int(h)]
        out.append({"id": win_id, "app": res_class, "title": title,
                    "rect": rect, "workspace": int(workspace),
                    "minimized": "_NET_WM_STATE_HIDDEN" in state,
                    "maximized": "_NET_WM_STATE_MAXIMIZED_VERT" in state and "_NET_WM_STATE_MAXIMIZED_HORZ" in state,
                    "ours": res_class.lower().startswith("agentbar"),
                    "slot": res_class[len("agentbar-"):] if res_class.startswith("agentbar-pane") else None})
    return sorted(out, key=lambda w: order.get(int(w["id"], 16), -1))
