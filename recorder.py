"""Trajectory recorder: each request as checkpoint -> op -> ... -> checkpoint, on disk.

  runs/<date>/<time>-<slug>/trajectory.jsonl   one JSON object per line, in `seq` order of
                                               creation (a checkpoint can be written after
                                               the next op, while it was being captured)
  runs/<date>/<time>-<slug>/cN.jpg             checkpoint thumbnails
  runs/<date>/<time>-<slug>/img-NNN.jpg        screenshots the agent saw

A checkpoint holds a full, restorable scene (dscene capture_scene): set_scene brings that
moment back. An op is an agent tool call, a model "think" step, or a user action:
  {"seq", "t", "type": "checkpoint", "id": "c2", "trigger": "after:set_deck", "scene", "thumbnail"}
  {"seq", "t", "type": "op", "actor": "agent" | "model" | "user", "name", ...}
User actions outside a request (flipping scenes, commands) go to runs/<date>/ui.jsonl.
runs/ is inside the shared ~/proj folder, so it can be read from the host too.
"""
import base64
import io
import itertools
import json
import re
import threading
import time
from pathlib import Path

RUNS = Path(__file__).resolve().parent / "runs"
THUMB_WIDTH = 480


def thumbnail(jpeg_bytes):
    """A small JPEG of a screenshot (about 20 KB)."""
    try:
        from PIL import Image
    except ImportError:
        return jpeg_bytes
    im = Image.open(io.BytesIO(jpeg_bytes))
    im.thumbnail((THUMB_WIDTH, THUMB_WIDTH))
    out = io.BytesIO()
    im.convert("RGB").save(out, "JPEG", quality=70)
    return out.getvalue()


class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.run_dir = None
        self.t0 = 0.0
        self.seq = itertools.count()
        self.images = 0
        self.checkpoints = 0

    def start(self, request, **fields):
        """Begin a run for one request; returns its directory."""
        with self.lock:
            slug = re.sub(r"[^a-z0-9]+", "-", request.lower())[:40].strip("-") or "request"
            self.run_dir = RUNS / time.strftime("%Y-%m-%d") / f"{time.strftime('%H%M%S')}-{slug}"
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self.t0 = time.time()
            self.seq = itertools.count()
            self.images = self.checkpoints = 0
        self.op("user", "request", text=request, **fields)
        return self.run_dir

    def reserve(self):
        """A sequence number for a record that will be written later (e.g. a checkpoint)."""
        return next(self.seq)

    def _write(self, record, seq=None):
        with self.lock:
            if self.run_dir:
                path = self.run_dir / "trajectory.jsonl"
                record = {"seq": next(self.seq) if seq is None else seq,
                          "t": round(time.time() - self.t0, 3), **record}
            else:
                path = RUNS / time.strftime("%Y-%m-%d") / "ui.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                record = {"t": time.strftime("%H:%M:%S"), **record}
            with path.open("a") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def op(self, actor, name, **fields):
        self._write({"type": "op", "actor": actor, "name": name, **fields})

    def checkpoint(self, trigger, scene=None, screenshot=None, error=None, seq=None):
        """Record a restorable scene (from capture_scene) and a thumbnail; returns its id."""
        with self.lock:
            if not self.run_dir:
                return None
            cid = f"c{self.checkpoints}"
            self.checkpoints += 1
            thumb = None
            if screenshot:
                thumb = f"{cid}.jpg"
                (self.run_dir / thumb).write_bytes(thumbnail(screenshot))
        self._write({"type": "checkpoint", "id": cid, "trigger": trigger, "scene": scene,
                     "thumbnail": thumb, **({"error": error} if error else {})}, seq)
        return cid

    def image(self, data, mime="image/jpeg"):
        """Save an image the agent saw (bytes or base64 text); returns its file name, or None."""
        with self.lock:
            if not self.run_dir:
                return None
            self.images += 1
            name = f"img-{self.images:03d}.{'png' if 'png' in mime else 'jpg'}"
            raw = base64.b64decode(data) if isinstance(data, str) else data
            (self.run_dir / name).write_bytes(raw)
            return name

    def end(self, **fields):
        self.op("agent", "end", seconds=round(time.time() - self.t0, 2), **fields)
        with self.lock:
            self.run_dir = None


RECORDER = Recorder()  # shared by the agent thread and the GTK thread
