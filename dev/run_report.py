#!/usr/bin/env python3
"""Summarise a recorded run (checkpoint -> op -> ... -> checkpoint), or restore a checkpoint.

    python3 dev/run_report.py                     # latest run
    python3 dev/run_report.py runs/2026-10-24/101500-show-me-how
    python3 dev/run_report.py --restore c2 [RUN]  # bring the desktop back to checkpoint c2
"""
import json
import subprocess
import sys
from pathlib import Path

RUNS = Path(__file__).resolve().parents[1] / "runs"
WAITS_FOR_USER = {"ask_user", "guide_step", "select_region"}


def latest():
    runs = sorted(p for p in RUNS.glob("*/*") if (p / "trajectory.jsonl").exists())
    if not runs:
        sys.exit("no runs recorded yet")
    return runs[-1]


def load(run):
    records = [json.loads(line) for line in (run / "trajectory.jsonl").open()]
    return sorted(records, key=lambda r: r["seq"])


def describe(scene):
    if not scene:
        return "nothing captured"
    if not scene["panes"]:
        return "empty desktop"
    panes = [f"{p['kind']}({p.get('fidelity', '?')})" for p in scene["panes"]]
    return f"{len(panes)} windows: {' '.join(panes)}"


def report(run):
    records = load(run)
    print(f"run: {run}")
    model_s = tool_s = wait_s = 0.0
    tokens_in = tokens_out = 0
    for r in records:
        t = r["t"]
        if r["type"] == "checkpoint":
            err = f"  ({r['error'][:60]})" if r.get("error") else ""
            print(f"{t:7.1f}s  ● {r['id']} [{r['trigger']}] {describe(r['scene'])}"
                  f"{'  ' + r['thumbnail'] if r.get('thumbnail') else ''}{err}")
            continue
        actor, name = r["actor"], r["name"]
        if actor == "model":
            if name != "think":
                print(f"{t:7.1f}s    model {name}")
                continue
            model_s += r["seconds"]
            usage = r.get("usage") or {}
            tokens_in += usage.get("total_input_tokens") or usage.get("prompt_tokens") or 0
            tokens_out += usage.get("total_output_tokens") or usage.get("completion_tokens") or 0
            decided = ", ".join(r["calls"]) if r["calls"] else f"reply: {r['text'][:60]!r}"
            print(f"{t:7.1f}s    think {r['seconds']:5.1f}s -> {decided}{'  (+web search)' if r.get('searched') else ''}")
        elif actor == "agent" and name == "end":
            print(f"{t:7.1f}s    end: {(r.get('reply') or r.get('error') or '')[:100]!r}")
        elif actor == "agent":
            if name in WAITS_FOR_USER:
                wait_s += r["seconds"]
            else:
                tool_s += r["seconds"]
            print(f"{t:7.1f}s    agent {name} {r['seconds']:.1f}s {json.dumps(r['args'])[:70]}")
        else:
            details = {k: v for k, v in r.items() if k not in ("seq", "t", "type", "actor", "name")}
            print(f"{t:7.1f}s    user  {name} {json.dumps(details, ensure_ascii=False)[:80]}")
    total = records[-1]["t"] if records else 0
    size = sum(f.stat().st_size for f in run.iterdir()) / 1024
    checkpoints = sum(r["type"] == "checkpoint" for r in records)
    print(f"\ntotal {total:.1f}s = model {model_s:.1f}s + tools {tool_s:.1f}s + waiting for the user "
          f"{wait_s:.1f}s + other {max(total - model_s - tool_s - wait_s, 0):.1f}s")
    print(f"{checkpoints} checkpoints, {len(records) - checkpoints} ops, {size:.0f} KB on disk"
          + (f"; tokens {tokens_in} in, {tokens_out} out" if tokens_in else ""))


def restore(run, cid):
    """set_scene with a checkpoint's scene, via the dscene server in the VM."""
    scene = next((r["scene"] for r in load(run) if r["type"] == "checkpoint" and r["id"] == cid), None)
    if not scene:
        sys.exit(f"no checkpoint {cid} with a scene in {run}")
    if not scene["panes"]:
        sys.exit(f"{cid} is an empty desktop: nothing to restore")
    arg = json.dumps({"scene": scene, "look": False})
    out = subprocess.run(["ssh", "archvm", "cd ~/proj/scenebar && ~/.venvs/scenebar/bin/python "
                          f"dev/dscene_call.py set_scene {json.dumps(arg)}"],
                         capture_output=True, text=True)
    print(out.stdout.strip() or out.stderr.strip()[-500:])


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--restore":
        restore(Path(args[2]) if len(args) > 2 else latest(), args[1])
    else:
        report(Path(args[0]) if args else latest())
