"""Any other app window: position and size only (fidelity "layout")."""
from dscene.adapters.base import find_window, place_window
from dscene.spec import Pane


class WindowAdapter:
    kind = "window"

    def matches(self, win):
        return True

    def capture(self, win):
        return Pane(kind="window", app=win["app"], title=win["title"], rect=win["rect"], fidelity="layout")

    def restore(self, pane, rect, windows_fn):
        win = find_window(windows_fn(), exact_title=pane.title, app=pane.app, title=pane.app)
        if not win:
            return {"kind": "window", "ok": False, "note": f"no open window matches {pane.app or pane.title!r}"}
        place_window(win["id"], rect, pane.maximized)
        return {"kind": "window", "window": win["id"], "ok": True, "fidelity": "layout"}

    place = restore
