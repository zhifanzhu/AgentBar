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


def frame_extents(win_id):
    """Decoration sizes (left, right, top, bottom) the window manager adds around a window."""
    out = sh("xprop", "-id", win_id, "_NET_FRAME_EXTENTS")
    try:
        return [int(v) for v in out.split("=", 1)[1].split(",")]
    except (IndexError, ValueError):
        return [0, 0, 0, 0]


def frame_rect(win_id):
    """The window as the user sees it, title bar included: [x, y, w, h] on screen.
    (wmctrl -lG reports positions shifted by the frame size, so it isn't used for this.)"""
    info = {}
    for line in sh("xwininfo", "-id", win_id).splitlines():
        key, _, value = line.strip().partition(":")
        info[key] = value.strip()
    try:
        x, y = int(info["Absolute upper-left X"]), int(info["Absolute upper-left Y"])
        w, h = int(info["Width"]), int(info["Height"])
    except (KeyError, ValueError):
        return None
    left, right, top, bottom = frame_extents(win_id)
    return [x - left, y - top, w + left + right, h + top + bottom]


def place_window(win_id, rect, maximized=False, activate=True):
    """Put a window's visible frame (title bar included) at rect = [x, y, w, h], or maximize it."""
    sh("wmctrl", "-i", "-r", win_id, "-b", "remove,hidden")
    if maximized:
        sh("wmctrl", "-i", "-r", win_id, "-b", "add,maximized_vert,maximized_horz")
    else:
        x, y, w, h = rect
        left, right, top, bottom = frame_extents(win_id)
        cw, ch = max(w - left - right, 50), max(h - top - bottom, 50)  # wmctrl sizes the contents
        for prop in ("maximized_vert", "maximized_horz"):  # xfwm4 ignores both in one request
            sh("wmctrl", "-i", "-r", win_id, "-b", f"remove,{prop}")
        sh("wmctrl", "-i", "-r", win_id, "-e", f"0,{x},{y},{cw},{ch}")
        time.sleep(0.15)
        got = frame_rect(win_id)  # correct whatever offset the window manager applied
        if got and (got[0], got[1]) != (x, y) and abs(got[0] - x) < 200 and abs(got[1] - y) < 200:
            sh("wmctrl", "-i", "-r", win_id, "-e", f"0,{2 * x - got[0]},{2 * y - got[1]},{cw},{ch}")
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
