#!/usr/bin/env python3
"""Compare grounding providers (find_element) on the current screen: speed, and how far
apart their answers are. Run in the VM session:
    ~/.venvs/scenebar/bin/python dev/grounding_bench.py ["description" ...]
"""
import statistics
import sys
from pathlib import Path

import gi

gi.require_version("Gdk", "3.0")
from gi.repository import Gdk  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dscene import grounding  # noqa: E402

DEFAULT = ["the File menu", "the Add Modifier button", "the wrench icon in the properties editor",
           "the play button in the timeline", "the Scripting workspace tab"]

root = Gdk.get_default_root_window()
W, H = root.get_width(), root.get_height()
png = bytes(Gdk.pixbuf_get_from_window(root, 0, 0, W, H).save_to_bufferv("png", [], [])[1])
times = {p: [] for p in grounding.PROVIDERS}
for d in sys.argv[1:] or DEFAULT:
    points = {}
    for provider in grounding.PROVIDERS:
        try:
            (x, y), seconds = grounding.localize(png, d, provider)
        except Exception as e:  # noqa: BLE001 - report and keep going
            print(f"  {provider:7s} ERROR {str(e)[:90]}")
            continue
        times[provider].append(seconds)
        points[provider] = (round(x / 1000 * W), round(y / 1000 * H))
    gap = ""
    if len(points) == 2:
        (ax, ay), (bx, by) = points.values()
        gap = f"   apart {((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5:.0f}px"
    print(f"{d[:42]:42s} " + "  ".join(f"{p} {points[p]} {times[p][-1]:.2f}s" for p in points) + gap)
for provider, ts in times.items():
    if ts:
        print(f"{provider:7s} median {statistics.median(ts):.2f}s over {len(ts)} lookups")
