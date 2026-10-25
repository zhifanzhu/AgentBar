#!/usr/bin/env python3
"""Play the user in a guided task, from vast: ask in the bar, follow each STEP, answer Done.

    python3 dev/tutorial_drive.py "show me how to add a bevel modifier to the cube in Blender"
    python3 dev/tutorial_drive.py --say "remember this screen"

Types only once the bar is confirmed focused (never into another app). Screenshots of each
step go to runs/drive-<time>/.
"""
import functools
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHOT = ROOT / "dev" / "vm-shot.sh"
X = "export DISPLAY=:0 XAUTHORITY=$HOME/.Xauthority; "
print = functools.partial(print, flush=True)  # noqa: A001 - progress as it happens


def vm(cmd):
    return subprocess.run(["ssh", "archvm", X + cmd], capture_output=True, text=True).stdout


def scene():
    out = vm("cd ~/proj/scenebar; ~/.venvs/scenebar/bin/python dev/dscene_call.py get_scene 2>/dev/null")
    try:
        return json.loads(out)
    except ValueError:
        return {}


def say_in_bar(text):
    """Type into the bar, but only once it's confirmed to have keyboard focus."""
    out = vm('W=$(xdotool search --onlyvisible --name "^SceneBar$" | head -1); '
             'if [ -z "$W" ]; then xdotool key F4; sleep 0.8; W=$(xdotool search --onlyvisible --name "^SceneBar$" | head -1); fi; '
             '[ -n "$W" ] && xdotool windowactivate --sync $W; sleep 0.4; '
             'if [ "$(xdotool getwindowfocus getwindowname)" = "SceneBar" ]; then '
             f'xdotool type --delay 15 {json.dumps(text)}; xdotool key Return; echo TYPED; else echo NOT-FOCUSED; fi')
    if "TYPED" not in out:
        raise SystemExit(f"bar not focused, refusing to type {text!r}")


def wait_until_idle(timeout=180):
    """Wait for the agent to finish (no 'Thinking' in the bar status for a few seconds)."""
    deadline, quiet = time.time() + timeout, 0
    while time.time() < deadline and quiet < 3:
        time.sleep(2)
        busy = "Thinking" in vm("tail -c 2000 /tmp/scenebar.log") or scene().get("asking")
        quiet = 0 if busy else quiet + 1


def drive(request, out):
    say_in_bar(request)
    print(f"{time.strftime('%T')} asked: {request}")
    handled, last_activity = set(), time.time()
    while time.time() - last_activity < 40:
        time.sleep(3)
        s = scene()
        asking, notes = s.get("asking"), s.get("annotations", [])
        if not asking:
            continue
        last_activity = time.time()
        key = (asking, tuple(a["id"] for a in notes))
        if key in handled:
            continue
        handled.add(key)
        n = len(handled)
        subprocess.run([str(SHOT), str(out / f"step-{n}.png")], capture_output=True)
        steps = [a for a in notes if a["kind"] == "step"]
        print(f"{time.strftime('%T')} step {n}: {asking!r}")
        if steps and n <= 8:
            x, y, w, h = max(steps, key=lambda a: a["id"])["rect"]
            vm(f"xdotool mousemove {x + w // 2} {y + h // 2} click 1")  # the user follows the step
            time.sleep(1.5)
        say_in_bar("Done" if n <= 8 else "Stop")
    subprocess.run([str(SHOT), str(out / "final.png")], capture_output=True)


if __name__ == "__main__":
    out = ROOT / "runs" / f"drive-{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    if sys.argv[1] == "--say":
        say_in_bar(sys.argv[2])
        wait_until_idle()
        subprocess.run([str(SHOT), str(out / "final.png")], capture_output=True)
        print(f"{time.strftime('%T')} done: {sys.argv[2]!r}")
    else:
        drive(sys.argv[1], out)
    print(f"screenshots in {out}")
