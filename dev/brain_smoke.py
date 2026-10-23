#!/usr/bin/env python3
"""Smoke-test the brain on a few typical requests. Run in the VM:
    python dev/brain_smoke.py [gemini|local]
Uses the saved config, with the provider overridden in memory only."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brain  # noqa: E402

REQUESTS = [
    "hotels near kings cross under 150 a night",
    "put this chart into my slides",
    "how do I add a bevel modifier in blender",
    "what is 15% of 340",
    "do the thing from before",
]

cfg = brain.load_config()
if len(sys.argv) > 1:
    cfg["provider"] = sys.argv[1]
print("brain:", brain.describe(cfg))
for q in REQUESTS:
    t0 = time.perf_counter()
    try:
        p = brain.plan(q, cfg)
    except brain.BrainError as e:
        print(f"{time.perf_counter() - t0:5.1f}s  {q:42s} -> ERROR {e}")
        continue
    print(f"{time.perf_counter() - t0:5.1f}s  {q:42s} -> {p['intent']}: {p['reply'][:80]}")
    for place in p["places"]:
        print(f"         · {place['name']} | {place['note']} | {place['url']} | {place['lat']},{place['lon']}")
    for i, step in enumerate(p["tutorial_steps"], 1):
        print(f"         {i}. {step}")
