"""What every adapter shares: the adapter interface and window helpers.

An app adapter handles one pane kind and implements:
  matches(window)                 -> is this live window mine?
  capture(window)                 -> Pane: a state descriptor for it (with rect and fidelity)
  restore(pane, rect, windows_fn) -> show the pane's state in a window placed at rect; a report
  place(pane, rect, windows_fn)   -> just find its window and place it (decks: no state change)
windows_fn() returns the current window list (dscene.desktop.list_windows).
"""
import subprocess
import time


def sh(*args):
    return subprocess.run(args, capture_output=True, text=True).stdout


def _geometry(win_id):
    for line in sh("wmctrl", "-lG").splitlines():
        parts = line.split()
        if parts and int(parts[0], 16) == int(win_id, 16):
            return [int(v) for v in parts[2:6]]
    return None


def place_window(win_id, rect, maximized=False, activate=True):
    """Put a window at rect = [x, y, w, h] as wmctrl reports it (or maximized)."""
    sh("wmctrl", "-i", "-r", win_id, "-b", "remove,hidden")
    if maximized:
        sh("wmctrl", "-i", "-r", win_id, "-b", "add,maximized_vert,maximized_horz")
    else:
        x, y, w, h = rect
        sh("wmctrl", "-i", "-r", win_id, "-b", "remove,maximized_vert,maximized_horz")
        sh("wmctrl", "-i", "-r", win_id, "-e", f"0,{x},{y},{w},{h}")
        time.sleep(0.15)
        got = _geometry(win_id)  # wmctrl -e places the frame; compensate the decoration offset
        if got and (got[0], got[1]) != (x, y) and abs(got[0] - x) < 100 and abs(got[1] - y) < 100:
            sh("wmctrl", "-i", "-r", win_id, "-e", f"0,{2 * x - got[0]},{2 * y - got[1]},{w},{h}")
    if activate:
        sh("wmctrl", "-i", "-a", win_id)


def find_window(windows, *, app=None, title=None, exact_title=None):
    """First non-agent window matching app (class) or title text, preferring an exact title."""
    candidates = [w for w in windows if not w["ours"]]
    if exact_title:
        for w in candidates:
            if w["title"] == exact_title:
                return w
    for w in candidates:
        if (app and app.lower() in w["app"].lower()) or (title and title.lower() in w["title"].lower()):
            return w
    return None
