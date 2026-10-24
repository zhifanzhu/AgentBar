#!/usr/bin/env python3
"""Can we capture (and send keys to) a window the user can't see? Run in the VM session:
    ~/.venvs/agentbar/bin/python dev/hidden_capture_test.py
Shows a patterned test window briefly, then hides it three ways and captures it by X id."""
import subprocess
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkX11", "3.0")
from gi.repository import Gdk, GdkX11, Gtk  # noqa: E402

W, H = 400, 300
keys = []
phase = [0]  # bumped while hidden, to see whether hidden windows still repaint


def draw(_w, cr):
    colours = [(1, 0, 0), (0, 0.8, 0), (0, 0, 1), (1, 0.8, 0)]
    colours = colours[phase[0] % 4:] + colours[:phase[0] % 4]
    for i, (r, g, b) in enumerate(colours):
        cr.set_source_rgb(r, g, b)
        cr.rectangle((i % 2) * W / 2, (i // 2) * H / 2, W / 2, H / 2)
        cr.fill()
    cr.set_source_rgb(1, 1, 1)
    cr.set_font_size(40)
    cr.move_to(60, 160)
    cr.show_text(f"keys: {len(keys)}")


def pump(seconds):
    end = time.time() + seconds
    while time.time() < end:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.02)


def sh(*args):
    return subprocess.run(args, capture_output=True, text=True).stdout.strip()


def capture(xid):
    """Pixels of window xid via its own X id (not the screen), as bytes, or None."""
    win = GdkX11.X11Window.foreign_new_for_display(Gdk.Display.get_default(), xid)
    pb = Gdk.pixbuf_get_from_window(win, 0, 0, W, H) if win else None
    return pb.get_pixels() if pb else None


def live_check(name, xid, ref):
    """Change the content while hidden: does the capture show the new frame?"""
    phase[0] += 1
    area.queue_draw()
    pump(0.8)
    data = capture(xid)
    changed = data is not None and sum(abs(a - b) for a, b in zip(data[::97], ref[::97])) / len(ref[::97]) > 20
    print(f"   {name}: repaints while hidden? {'YES (live pixels)' if changed else 'NO (stale or blank)'}")
    phase[0] -= 1
    area.queue_draw()
    pump(0.5)


def compare(name, data, ref):
    if data is None:
        print(f"{name:34s} capture FAILED (no pixels)")
        return
    diff = sum(abs(a - b) for a, b in zip(data[::97], ref[::97])) / len(ref[::97])
    nonblack = sum(1 for v in data[::97] if v > 10) / len(data[::97])
    verdict = "identical" if diff < 2 else "DIFFERENT"
    print(f"{name:34s} {verdict:9s} (mean diff {diff:5.1f}/255, non-black {nonblack:.0%})")


win = Gtk.Window(title="hidden-capture-test")
win.set_default_size(W, H)
area = Gtk.DrawingArea()
area.connect("draw", draw)
win.add(area)
win.connect("key-press-event", lambda *_: (keys.append(1), area.queue_draw()))
win.move(100, 100)
win.show_all()
pump(1.5)
xid = win.get_window().get_xid()
hex_id = hex(xid)
current = int(sh("xdotool", "get_desktop"))
empty = next(d for d in range(int(sh("xdotool", "get_num_desktops"))) if d != current
             and not [l for l in sh("wmctrl", "-l").splitlines() if l.split()[1] == str(d)])
print(f"user workspace {current}, empty workspace used for the test: {empty}")

ref = capture(xid)
compare("1. visible (reference)", ref, ref)

sh("xdotool", "set_desktop_for_window", str(xid), str(empty))
pump(1.0)
compare("2. on another workspace", capture(xid), ref)
live_check("another workspace", xid, ref)
sh("xdotool", "key", "--window", str(xid), "a")
pump(0.5)
print(f"   key sent with --window while hidden: received {len(keys)}")

sh("xdotool", "set_desktop_for_window", str(xid), str(current))
sh("wmctrl", "-i", "-r", hex_id, "-e", f"0,-4000,-4000,{W},{H}")
pump(1.0)
compare("3. off-screen (-4000,-4000)", capture(xid), ref)
live_check("off-screen", xid, ref)

sh("wmctrl", "-i", "-r", hex_id, "-e", f"0,100,100,{W},{H}")
pump(0.5)
sh("xdotool", "windowminimize", str(xid))
pump(1.0)
compare("4. minimized", capture(xid), ref)
live_check("minimized", xid, ref)

win.destroy()
pump(0.3)
