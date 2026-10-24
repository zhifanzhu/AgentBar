"""dscene bridge inside Blender: capture and restore Blender's state over a local socket.

Installed as a Blender startup script (a symlink in ~/.config/blender/<version>/scripts/
startup/), so every Blender started afterwards listens on 127.0.0.1:8766. Protocol: one
JSON request per connection, one JSON reply. Requests run on Blender's main thread, from
a timer, so they may use bpy freely.

  {"cmd": "ping"}
  {"cmd": "capture"}             -> {"blend": checkpoint path, "selected", "active", "mode",
                                     "view": [loc x,y,z, rot w,x,y,z, distance], "properties_tab"}
  {"cmd": "restore", "state": {...}}  same fields; with "blend", the current state is captured
                                      first (returned as "previous") and the checkpoint opened
"""
import json
import socket
import time
from pathlib import Path

import bpy

PORT = 8766
BLOBS = Path.home() / ".local" / "share" / "dscene" / "blobs"
_server = None
_pending = None  # state to apply once a checkpoint file has finished loading


def _override():
    win = bpy.context.window_manager.windows[0]
    return {"window": win, "screen": win.screen}


def _spaces(kind):
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == kind:
                yield area, area.spaces.active


def capture():
    BLOBS.mkdir(parents=True, exist_ok=True)
    path = BLOBS / f"blender-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.blend"
    with bpy.context.temp_override(**_override()):
        bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True)  # copy: the open file keeps its path
    layer = bpy.context.view_layer
    active = layer.objects.active
    state = {"blend": str(path), "file": bpy.data.filepath or None,
             "selected": [o.name for o in layer.objects if o.select_get()],
             "active": active.name if active else None,
             "mode": active.mode if active else "OBJECT"}
    for _area, space in _spaces("VIEW_3D"):
        r3d = space.region_3d
        state["view"] = [*r3d.view_location, *r3d.view_rotation, r3d.view_distance]
        break
    for _area, space in _spaces("PROPERTIES"):
        state["properties_tab"] = space.context
        break
    return state


def apply(state):
    """Selection, mode, 3D view and properties tab: everything except which file is open."""
    layer = bpy.context.view_layer
    if state.get("selected") is not None:
        for obj in layer.objects:
            obj.select_set(obj.name in state["selected"])
    if state.get("active") in layer.objects:
        layer.objects.active = layer.objects[state["active"]]
    active = layer.objects.active
    if state.get("mode") and active and active.mode != state["mode"]:
        with bpy.context.temp_override(**_override(), active_object=active):
            bpy.ops.object.mode_set(mode=state["mode"])
    view = state.get("view")
    for area, space in _spaces("VIEW_3D"):
        if view and len(view) == 8:
            space.region_3d.view_location = view[0:3]
            space.region_3d.view_rotation = view[3:7]
            space.region_3d.view_distance = view[7]
        area.tag_redraw()
    for area, space in _spaces("PROPERTIES"):
        if state.get("properties_tab"):
            try:
                space.context = state["properties_tab"]
            except TypeError:
                pass  # that tab doesn't exist for the active object
        area.tag_redraw()


def restore(state):
    global _pending
    if not state.get("blend"):
        apply(state)
        return {"ok": True}
    previous = capture()  # so this restore can itself be undone
    _pending = state
    with bpy.context.temp_override(**_override()):
        bpy.ops.wm.open_mainfile(filepath=state["blend"], load_ui=False)  # keep the window layout
    return {"ok": True, "previous": previous}


@bpy.app.handlers.persistent
def _after_load(*_args):
    global _pending
    if _pending:
        state, _pending = _pending, None
        bpy.app.timers.register(lambda: apply(state), first_interval=0.1)


def _handle(request):
    cmd = request.get("cmd")
    if cmd == "ping":
        return {"ok": True, "blender": bpy.app.version_string, "file": bpy.data.filepath or None}
    if cmd == "capture":
        return {"ok": True, **capture()}
    if cmd == "restore":
        return restore(request.get("state") or {})
    return {"ok": False, "error": f"unknown cmd {cmd!r}"}


def _poll():
    try:
        conn, _ = _server.accept()
    except BlockingIOError:
        return 0.1
    with conn:
        conn.settimeout(5)
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        try:
            reply = _handle(json.loads(data or b"{}"))
        except Exception as e:  # noqa: BLE001 - report every failure to the caller
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        conn.sendall((json.dumps(reply) + "\n").encode())
    return 0.02


def register():
    global _server
    try:
        _server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _server.bind(("127.0.0.1", PORT))
        _server.listen(4)
        _server.setblocking(False)
    except OSError:
        _server = None  # another Blender already has the bridge
        return
    bpy.app.timers.register(_poll, persistent=True)
    bpy.app.handlers.load_post.append(_after_load)


def unregister():
    if _server:
        _server.close()
