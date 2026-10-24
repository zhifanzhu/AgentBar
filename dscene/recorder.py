"""Trajectory recorder: what the agent and the user did, saved lightly for later analysis.

Each request becomes a run:
  runs/<date>/<time>-<slug>/events.jsonl   one JSON object per line: t (s since start), type, ...
  runs/<date>/<time>-<slug>/img-NNN.jpg    screenshots, as the agent saw them
Bar events outside a run (flipping scenes, summoning the bar) go to runs/<date>/ui.jsonl.

runs/ is inside the shared ~/proj folder, so it can be read from the host too.
"""
import base64
import json
import re
import threading
import time
from pathlib import Path

RUNS = Path(__file__).resolve().parents[1] / "runs"


class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.run_dir = None
        self.t0 = 0.0
        self.images = 0

    def start(self, request, **fields):
        """Begin a run for one request; returns its directory."""
        with self.lock:
            slug = re.sub(r"[^a-z0-9]+", "-", request.lower())[:40].strip("-") or "request"
            self.run_dir = RUNS / time.strftime("%Y-%m-%d") / f"{time.strftime('%H%M%S')}-{slug}"
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self.t0 = time.time()
            self.images = 0
        self.event("request", text=request, **fields)
        return self.run_dir

    def event(self, kind, **fields):
        with self.lock:
            if self.run_dir:
                path, t = self.run_dir / "events.jsonl", round(time.time() - self.t0, 3)
            else:
                path, t = RUNS / time.strftime("%Y-%m-%d") / "ui.jsonl", time.strftime("%H:%M:%S")
                path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as f:
                f.write(json.dumps({"t": t, "type": kind, **fields}, ensure_ascii=False, default=str) + "\n")

    def image(self, data, mime="image/jpeg"):
        """Save an image (bytes or base64 text) into the run; returns its file name, or None."""
        with self.lock:
            if not self.run_dir:
                return None
            self.images += 1
            name = f"img-{self.images:03d}.{'png' if 'png' in mime else 'jpg'}"
            raw = base64.b64decode(data) if isinstance(data, str) else data
            (self.run_dir / name).write_bytes(raw)
            return name

    def end(self, **fields):
        self.event("end", seconds=round(time.time() - self.t0, 2), **fields)
        with self.lock:
            self.run_dir = None


RECORDER = Recorder()  # shared by the agent thread and the GTK thread
