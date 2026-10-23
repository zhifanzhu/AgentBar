#!/usr/bin/env python3
"""Closed-loop check: Holo finds each control on a fresh screenshot, we click it.

Goal: add a Bevel modifier to the selected cube in Blender, in the VM.
Each step: screenshot -> ask Holo for the target -> xdotool click -> wait.
Saves step-N.png with the click point marked, for checking afterwards.
"""
import base64
import subprocess
import time
from pathlib import Path

from ground_eval import image_size, localize

URL, MODEL = "http://127.0.0.1:8000/v1", "holo3-1-9b"
OUT = Path(__file__).parent / "bevel-demo"
SHOT = Path(__file__).resolve().parents[1] / "dev" / "vm-shot.sh"
STEPS = [
    "the modifier properties tab (wrench icon) in the properties editor",
    "the 'Add Modifier' dropdown button in the modifier properties",
    "the 'Generate' category in the open Add Modifier menu",
    "the 'Bevel' item in the open menu",
]


def vm(cmd):
    subprocess.run(["ssh", "archvm", f"DISPLAY=:0 XAUTHORITY=$HOME/.Xauthority {cmd}"], check=True)


def main():
    OUT.mkdir(exist_ok=True)
    vm('wmctrl -a "Blender"')
    time.sleep(1)
    for i, desc in enumerate(STEPS, 1):
        shot = OUT / f"step-{i}.png"
        subprocess.run([str(SHOT), str(shot)], check=True, capture_output=True)
        w, h = image_size(shot)
        uri = "data:image/png;base64," + base64.b64encode(shot.read_bytes()).decode()
        t0 = time.perf_counter()
        point, _ = localize(URL, MODEL, uri, desc)
        ms = (time.perf_counter() - t0) * 1000
        x, y = round(point["x"] / 1000 * w), round(point["y"] / 1000 * h)
        print(f"step {i}: {desc!r} -> click ({x}, {y})  [{ms:.0f} ms]")
        subprocess.run(["convert", str(shot), "-fill", "none", "-stroke", "#ff00ff", "-strokewidth", "3",
                        "-draw", f"circle {x},{y} {x + 10},{y}", str(shot)], check=True)
        vm(f"xdotool mousemove {x} {y} click 1")
        time.sleep(1.5)
    final = OUT / "final.png"
    subprocess.run([str(SHOT), str(final)], check=True, capture_output=True)
    print(f"final screenshot: {final}")


if __name__ == "__main__":
    main()
