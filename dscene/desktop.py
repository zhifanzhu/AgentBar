"""Scene reconciliation: layouts, decks, set_scene and capture_scene.

Browser panes go into the agent's Chromium slots (adapters/browser.py); app panes are
the user's own windows, restored by their adapters (adapters/blender.py, impress.py,
window.py). A deck preloads its browser tabs, so swapping scenes is only tab activation.
"""
from dscene.adapters import APP_ADAPTERS, adapter_for
from dscene.adapters.base import frame_rect, sh
from dscene.adapters.browser import ChromeSlot, set_scroll
from dscene.spec import BROWSER_KINDS, Pane, SceneSpec, layout_rects

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
            slot.start()
        stale = {slot.role: [t["id"] for t in slot.pages()] for slot in used}
        self.tabs = [[used[j].open(url) for j, url in enumerate(s.urls())] for s in scenes]
        self.scenes = scenes
        for slot in self.slots:
            for target_id in stale.get(slot.role, []):
                slot.close(target_id)
            slot.hide()

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
