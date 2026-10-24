#!/usr/bin/env python3
"""Summarise a recorded agent run: timeline plus where the time went.

    python3 dev/run_report.py              # latest run
    python3 dev/run_report.py runs/2026-10-24/101500-show-me-how
"""
import json
import sys
from pathlib import Path

RUNS = Path(__file__).resolve().parents[1] / "runs"


def latest():
    runs = sorted(p for p in RUNS.glob("*/*") if (p / "events.jsonl").exists())
    if not runs:
        sys.exit("no runs recorded yet")
    return runs[-1]


def main():
    run = Path(sys.argv[1]) if len(sys.argv) > 1 else latest()
    events = [json.loads(line) for line in (run / "events.jsonl").open()]
    print(f"run: {run}")
    model_s = tool_s = wait_s = 0.0
    tokens_in = tokens_out = 0
    for e in events:
        t, kind = e["t"], e["type"]
        if kind == "model":
            model_s += e["seconds"]
            usage = e.get("usage") or {}
            tokens_in += usage.get("total_input_tokens") or usage.get("prompt_tokens") or 0
            tokens_out += usage.get("total_output_tokens") or usage.get("completion_tokens") or 0
            steps = e["response"].get("steps") or [
                {"type": "function_call", "name": c["function"]["name"]}
                for c in (e["response"].get("choices", [{}])[0].get("message", {}).get("tool_calls") or [])]
            calls = [s.get("name") for s in steps if s.get("type") == "function_call"]
            searched = any(s.get("type") == "google_search_call" for s in steps)
            print(f"{t:7.1f}s  model {e['seconds']:5.1f}s  -> "
                  f"{', '.join(calls) if calls else 'final reply'}{'  (+web search)' if searched else ''}")
        elif kind == "tool":
            waited = e["name"] in ("ask_user", "guide_step", "select_region")
            (tool_s, wait_s) = (tool_s, wait_s + e["seconds"]) if waited else (tool_s + e["seconds"], wait_s)
            images = f"  [{', '.join(e['images'])}]" if e.get("images") else ""
            print(f"{t:7.1f}s  tool  {e['seconds']:5.1f}s  {e['name']} {json.dumps(e['args'])[:80]}{images}")
        elif kind == "request":
            print(f"{t:7.1f}s  request: {e['text']!r} ({e.get('model')})")
        elif kind == "end":
            print(f"{t:7.1f}s  end: {(e.get('reply') or e.get('error') or '')[:120]!r}")
        elif kind == "snapshot":
            print(f"{t:7.1f}s  snapshot {e['when']}: {len(e.get('windows', []))} windows, "
                  f"screenshot {e.get('screenshot')}")
    total = events[-1]["t"] if events else 0
    print(f"\ntotal {total:.1f}s = model {model_s:.1f}s + tools {tool_s:.1f}s + waiting for the user "
          f"{wait_s:.1f}s + other {max(total - model_s - tool_s - wait_s, 0):.1f}s")
    if tokens_in or tokens_out:
        print(f"tokens: {tokens_in} in, {tokens_out} out")


if __name__ == "__main__":
    main()
