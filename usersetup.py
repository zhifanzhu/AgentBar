#!/usr/bin/env python3
"""Per-user setup for SceneBar: `scenebar setup` and `scenebar uninstall` (bin/scenebar).

setup      checks what's installed, links dscene's Blender bridge into each Blender
           version's startup scripts, adds SceneBar to the app menu (when run from a
           checkout) and binds F4 to scenebar-toggle on XFCE. Safe to run again, e.g.
           after a Blender upgrade.
uninstall  stops the bar, undoes setup, then shows SceneBar's settings and data with their
           sizes and asks before deleting each (--yes: delete all, --keep-data: keep all).

Needs only the standard library, so it works even when SceneBar's dependencies don't.
Nothing here needs root: a system package installs the files, this wires them up per user.
"""
import argparse
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOME = Path.home()
TOGGLE = ROOT / "bin" / "scenebar-toggle"
BRIDGE = ROOT / "dscene" / "adapters" / "blender_bridge.py"
BRIDGE_LINK = "dscene_bridge.py"  # its name in Blender's scripts/startup
BLENDER_CONFIG = HOME / ".config" / "blender"
MENU_ENTRY = HOME / ".local" / "share" / "applications" / "scenebar.desktop"
SYSTEM_MENU_ENTRY = Path("/usr/share/applications/scenebar.desktop")
SHORTCUTS = ["xfconf-query", "-c", "xfce4-keyboard-shortcuts"]
APP_ID = "dev.scenebar.SceneBar"
BROWSER_PROFILES = HOME / ".cache" / "dscene"
# Everything SceneBar and dscene write, as (folder, what's in it). Mirrors brain.CONFIG,
# dscene/config.py, the browser profiles and captures, dscene/spec.py's decks and blobs,
# and recorder.RUNS.
DATA = [
    (HOME / ".config" / "scenebar", "settings, your notes (USER.md), the API key"),
    (HOME / ".config" / "dscene", "dscene settings (may hold the API key)"),
    (BROWSER_PROFILES, "the agent's browser profiles (cookies, sign-ins) and captures"),
    (HOME / ".local" / "share" / "dscene", "saved decks, Blender and Impress checkpoints"),
    (HOME / ".local" / "state" / "scenebar", "recordings of each request, with screenshots"),
]

# What SceneBar uses, with the Arch package that provides it.
COMMANDS = [("wmctrl", "wmctrl"), ("xdotool", "xdotool"), ("xprop", "xorg-xprop"),
            ("xwininfo", "xorg-xwininfo"), ("xclip", "xclip"), ("chromium", "chromium"),
            ("gdbus", "glib2")]
OPTIONAL_COMMANDS = [("blender", "blender", "Blender lessons and blender_run"),
                     ("soffice", "libreoffice-fresh", "screenshots into Impress slides"),
                     ("xfce4-terminal", "xfce4-terminal", "open_terminal and read_terminal")]
MODULES = [("gi", "python-gobject"), ("cairo", "python-cairo"), ("mcp", "python-mcp"),
           ("pydantic", "python-pydantic"), ("websocket", "python-websocket-client"),
           ("PIL", "python-pillow")]


def say(mark, text):
    print(f"  {mark} {text}")


def run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(cmd, 127, "", "")


# -- setup ---------------------------------------------------------------------
def check():
    """Report what's missing; returns the Arch packages that would fix it."""
    print("Checking what's installed")
    missing = []
    for cmd, pkg in COMMANDS:
        if shutil.which(cmd):
            say("✓", cmd)
        else:
            say("✗", f"{cmd} (needed; Arch package {pkg})")
            missing.append(pkg)
    for cmd, pkg, use in OPTIONAL_COMMANDS:
        if shutil.which(cmd):
            say("✓", cmd)
        else:
            say("–", f"{cmd} (optional, for {use}; Arch package {pkg})")
    for name, pkg in MODULES:
        if importlib.util.find_spec(name) is None:
            say("✗", f"Python module {name} (Arch package {pkg})")
            missing.append(pkg)
    if importlib.util.find_spec("mcp") and not version("mcp").startswith("1."):
        say("✗", f"mcp {version('mcp')}: SceneBar needs the 1.x SDK (pip install 'mcp>=1.29,<2')")
        missing.append("python-mcp")
    if importlib.util.find_spec("gi"):
        import gi
        for lib, ver, pkg in (("Gtk", "3.0", "gtk3"), ("Atspi", "2.0", "at-spi2-core")):
            try:
                gi.require_version(lib, ver)
            except ValueError:
                say("✗", f"{lib} {ver} introspection data (Arch package {pkg})")
                missing.append(pkg)
    session = os.environ.get("XDG_SESSION_TYPE", "")
    if session and session != "x11":
        say("✗", f"this is a {session} session: SceneBar needs X11")
    if not gemini_key_set():
        say("–", "no Gemini key yet: start the bar and type /key gemini YOUR_KEY "
                 "(get one at https://aistudio.google.com/apikey)")
    if not missing:
        say("✓", "everything SceneBar needs is installed")
    return sorted(set(missing))


def _read(path):
    try:
        return path.read_text()
    except OSError:
        return ""


def gemini_key_set():
    """Is there a Gemini key in the environment, brain.json or dscene's config?"""
    if os.environ.get("GEMINI_API_KEY"):
        return True
    for config in (DATA[0][0] / "brain.json", DATA[1][0] / "config.json"):
        try:
            if json.loads(_read(config) or "{}").get("gemini_api_key"):
                return True
        except ValueError:
            pass
    return False


def blender_versions():
    """Blender versions to link the bridge for: the installed one, plus any with a config."""
    versions = {p.name for p in BLENDER_CONFIG.glob("*") if re.fullmatch(r"\d+\.\d+", p.name)}
    found = re.search(r"Blender (\d+\.\d+)", run("blender", "--version").stdout)
    if found:
        versions.add(found.group(1))
    return sorted(versions)


def link_blender_bridge():
    print("Blender bridge (lets SceneBar read and restore Blender, and run blender_run)")
    versions = blender_versions()
    if not versions:
        say("–", "Blender isn't installed: skipped")
        return
    for v in versions:
        link = BLENDER_CONFIG / v / "scripts" / "startup" / BRIDGE_LINK
        if link.is_symlink() and link.resolve() == BRIDGE:
            say("✓", f"Blender {v}: already linked")
            continue
        if link.exists() and not link.is_symlink():
            say("✗", f"Blender {v}: {link} is a file, not our link: left alone")
            continue
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            link.unlink()  # ours, from an older checkout or install
        link.symlink_to(BRIDGE)
        say("✓", f"Blender {v}: linked {link}")
    if run("pgrep", "-x", "blender").returncode == 0:
        say("!", "a Blender that's already open loads the bridge when it next starts")


def add_menu_entry():
    print("App menu")
    if SYSTEM_MENU_ENTRY.exists():
        say("✓", f"installed system-wide ({SYSTEM_MENU_ENTRY})")
        return
    entry = (ROOT / "scenebar.desktop").read_text().replace("Exec=scenebar-toggle", f"Exec={TOGGLE}")
    MENU_ENTRY.parent.mkdir(parents=True, exist_ok=True)
    MENU_ENTRY.write_text(entry)
    say("✓", f"SceneBar is in the app menu ({MENU_ENTRY})")


def bindings():
    """XFCE custom shortcuts that run scenebar-toggle: {property: command}."""
    out = run(*SHORTCUTS, "-l", "-v").stdout
    found = {}
    for line in out.splitlines():
        prop, _, command = line.partition(" ")
        if prop.startswith("/commands/custom/") and command.strip().endswith("scenebar-toggle"):
            found[prop] = command.strip()
    return found


def bind_key(key):
    print(f"Hotkey ({key} shows and hides the bar)")
    if not shutil.which("xfconf-query") or "XFCE" not in os.environ.get("XDG_CURRENT_DESKTOP", "XFCE"):
        say("–", f"not XFCE: bind {key} to {TOGGLE} in your desktop's keyboard settings")
        return
    prop = f"/commands/custom/{key}"
    current = run(*SHORTCUTS, "-p", prop)
    command = current.stdout.strip()
    if current.returncode == 0 and command == str(TOGGLE):
        say("✓", f"{key} already runs {TOGGLE}")
        return
    if current.returncode == 0 and command and not command.endswith("scenebar-toggle"):
        say("✗", f"{key} already runs {command!r}: left alone. Pick another key, "
                 f"e.g. scenebar setup --key '<Super>space'")
        return
    create = [] if current.returncode == 0 else ["-n", "-t", "string"]  # -n only for a new one
    r = run(*SHORTCUTS, "-p", prop, *create, "-s", str(TOGGLE))
    say("✓" if r.returncode == 0 else "✗",
        f"{key} runs {TOGGLE}" if r.returncode == 0 else f"couldn't bind {key}: {r.stderr.strip()}")


def setup(args):
    print(f"SceneBar setup for {os.environ.get('USER', 'this user')} ({ROOT})\n")
    missing = check()
    print()
    link_blender_bridge()
    print()
    add_menu_entry()
    print()
    bind_key(args.key)
    print()
    if missing:
        print(f"Install what's missing first (Arch): sudo pacman -S {' '.join(missing)}")
    else:
        print(f"Done. Press {args.key} to open the bar (the first press starts it).")


# -- uninstall -----------------------------------------------------------------
def size(path):
    total = 0
    for dirpath, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    for unit in ("B", "KB", "MB", "GB"):
        if total < 1024 or unit == "GB":
            return f"{total:.0f} {unit}" if unit == "B" else f"{total:.1f} {unit}"
        total /= 1024


def stop_bar():
    """Stop the running bar (found by its D-Bus name, so nothing else is hit)."""
    r = run("gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path",
            "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.GetConnectionUnixProcessID", APP_ID)
    pid = re.search(r"uint32 (\d+)", r.stdout)
    if pid:
        os.kill(int(pid.group(1)), signal.SIGTERM)
        for _ in range(30):  # let it exit before its files go
            if not Path(f"/proc/{pid.group(1)}").exists():
                break
            time.sleep(0.1)
        say("✓", f"stopped the running bar (pid {pid.group(1)})")


def profiles_in_use():
    """Processes still using the agent's browser profiles (its Chromium windows)."""
    users = []
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            cmdline = (proc / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(arg.startswith(f"--user-data-dir={BROWSER_PROFILES}/".encode()) for arg in cmdline):
            users.append(proc.name)
    return users


def ask(question):
    try:
        return input(f"  {question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def uninstall(args):
    print(f"Removing SceneBar for {os.environ.get('USER', 'this user')}\n")
    stop_bar()
    for link in BLENDER_CONFIG.glob(f"*/scripts/startup/{BRIDGE_LINK}"):
        if link.is_symlink():
            link.unlink()
            say("✓", f"removed the Blender bridge link {link}")
    for prop, command in bindings().items():
        run(*SHORTCUTS, "-p", prop, "-r")
        say("✓", f"removed the shortcut {prop.removeprefix('/commands/custom/')} → {command}")
    if MENU_ENTRY.exists() and "Name=SceneBar" in _read(MENU_ENTRY):
        MENU_ENTRY.unlink()
        say("✓", f"removed the app menu entry {MENU_ENTRY}")

    present = [(path, what) for path, what in DATA if path.exists()]
    if present:
        print("\nSettings and data:")
    for path, what in present:
        label = f"{path} ({size(path)}): {what}"
        if args.keep_data:
            say("–", f"kept {label}")
            continue
        if path == BROWSER_PROFILES and profiles_in_use():
            say("!", f"kept {label}: SceneBar's browser windows are still open; close them "
                     "and run scenebar uninstall again to delete it")
            continue
        if args.yes or (sys.stdin.isatty() and ask(f"Delete {label}?")):
            shutil.rmtree(path)
            say("✓", f"deleted {path}")
        else:
            say("–", f"kept {label}" if not sys.stdin.isatty() else f"kept {path}")
    if present and not (args.yes or args.keep_data or sys.stdin.isatty()):
        print("  (run it in a terminal to choose what to delete, or add --yes to delete all of it)")
    print("\nWhat's left is the program itself: remove its package (e.g. pacman -R scenebar) "
          f"or delete {ROOT}.")


def main():
    parser = argparse.ArgumentParser(prog="scenebar")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("setup", help="check dependencies, link the Blender bridge, bind the hotkey")
    p.add_argument("--key", default="F4", help="hotkey in XFCE's notation, e.g. F4 or '<Super>space'")
    p = sub.add_parser("uninstall", help="undo setup, then offer to delete settings and data")
    p.add_argument("--yes", action="store_true", help="delete settings and data without asking")
    p.add_argument("--keep-data", action="store_true", help="keep settings and data")
    args = parser.parse_args()
    (setup if args.command == "setup" else uninstall)(args)


if __name__ == "__main__":
    main()
