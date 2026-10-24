"""Terminal: open a terminal window with a command typed at the prompt, not yet run.

The user reads it, edits it if they like, and presses Enter to run it (Ctrl+C drops it).
It's an ordinary interactive bash from the start, so editing, history and Ctrl+C behave
as usual. Nothing is typed with fake key events and nothing runs without the user's Enter.

How the command gets onto the prompt: the shell's rc file binds a readline macro to the
terminal's "status OK" reply (ESC [ 0 n), then asks the terminal for its status
(ESC [ 5 n). The terminal's reply arrives as input, readline expands the macro, and the
command sits on the prompt line.
"""
import os
import shlex
import subprocess
import tempfile
import time

from dscene.adapters.base import find_window, place_window, sh


def _rcfile(command):
    macro = command.replace("\\", "\\\\").replace('"', '\\"')
    fd, path = tempfile.mkstemp(prefix="agent-terminal-", suffix=".rc")
    with os.fdopen(fd, "w") as f:
        f.write(f"rm -f {shlex.quote(path)}\n"
                "[ -f ~/.bashrc ] && . ~/.bashrc\n"
                r"printf '\033[2mTyped by the agent: Enter runs it, Ctrl+C cancels.\033[0m\n'" "\n"
                f"bind {shlex.quote(f'\"\\e[0n\": \"{macro}\"')}\n"
                r"printf '\033[5n'" "\n")
    return path


def open_with_command(command, cwd=None, rect=None, windows_fn=None):
    """Open xfce4-terminal with command waiting at the prompt; returns its window id or None."""
    if "\n" in command:
        raise ValueError("one command line only (join steps with && or ;)")
    before = {w["id"] for w in windows_fn()} if windows_fn else set()
    subprocess.Popen(["xfce4-terminal", "--disable-server", "-T", "Terminal · from the agent",
                      f"--working-directory={os.path.expanduser(cwd or '~')}",
                      "-x", "bash", "--rcfile", _rcfile(command), "-i"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(40):
        time.sleep(0.1)
        new = [w for w in windows_fn() if w["id"] not in before] if windows_fn else []
        win = find_window(new, app="terminal")
        if win:
            if rect:
                place_window(win["id"], rect)
            subprocess.run(["wmctrl", "-i", "-a", win["id"]])  # keys go to it: Enter runs
            return win["id"]
    return None


def _terminals(node, depth=0):
    """Every widget with the terminal role under an accessibility node."""
    if depth > 12:
        return
    if node.get_role_name() == "terminal":
        yield node
    for i in range(node.get_child_count()):
        child = node.get_child_at_index(i)
        if child:
            yield from _terminals(child, depth + 1)


def _frame(node):
    """The window (frame) an accessibility node is in."""
    while node and node.get_role_name() != "frame":
        node = node.get_parent()
    return node


def read_text(windows, lines=60):
    """The text of each terminal tab, exactly as shown, via the accessibility tree (AT-SPI):
    VTE terminals publish their contents there for screen readers. windows: the window list,
    to tie each tab to its window id (by process and title). Visible tabs come first."""
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    window_of = {}  # (pid, title) -> window id; agent terminals each have their own process
    for w in windows:
        if "terminal" in w["app"].lower():
            pid = sh("xprop", "-id", w["id"], "_NET_WM_PID").rpartition("=")[2].strip()
            window_of[(int(pid) if pid.isdigit() else None, w["title"])] = w["id"]
    found = []
    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if not app or app.get_name() != "xfce4-terminal":
            continue
        for term in _terminals(app):
            frame = _frame(term)
            title = frame.get_name() if frame else ""
            showing = term.get_state_set().contains(Atspi.StateType.SHOWING)
            text = Atspi.Text.get_text(term, 0, Atspi.Text.get_character_count(term))
            kept = text.rstrip().splitlines()[-lines:]
            win = window_of.get((app.get_process_id(), title))
            found.append({"window": win, "title": title, "visible_tab": showing,
                          "text": "\n".join(kept)})
    found.sort(key=lambda t: not t["visible_tab"])
    return found
