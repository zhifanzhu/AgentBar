#!/usr/bin/env python3
"""Time dscene tools over MCP, the way the brain calls them: median and slowest of N calls.
Used to compare before/after moving code across the dscene / agentBar boundary.
Run in the VM while the bar is up:  ~/.venvs/agentbar/bin/python dev/boundary_bench.py [N]
It shows a test page in the agent's first browser window and draws (then removes) a label.
"""
import asyncio
import json
import statistics
import sys
import time

from mcp import Client

URL = "http://127.0.0.1:8765/mcp"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
CALLS = [
    ("get_scene", {}),
    ("capture_scene", {}),
    ("get_screen", {}),
    ("read_terminal", {"lines": 5}),
    ("get_selection", {}),
    ("annotate", {"text": "bench", "kind": "label", "target": {"region": [20, 600, 40, 40]}, "seconds": 0}),
    ("clear_annotations", {}),
    ("set_scene", {"scene": {"name": "bench", "panes": [{"kind": "url", "url": "https://example.com/"}]},
                   "look": False}),
]


async def main():
    async with Client(URL) as c:
        await c.call_tool("get_scene", {})  # warm up the session
        print(f"{'tool':18s} {'median':>8s} {'slowest':>8s}   ({N} calls each)")
        results = {}
        for name, args in CALLS:
            times, errors = [], 0
            for _ in range(N):
                t0 = time.perf_counter()
                result = await c.call_tool(name, args)
                times.append((time.perf_counter() - t0) * 1000)
                errors += bool(getattr(result, "is_error", None) or getattr(result, "isError", False))
            results[name] = {"median_ms": round(statistics.median(times), 1), "max_ms": round(max(times), 1),
                             "errors": errors}
            print(f"{name:18s} {results[name]['median_ms']:7.1f}ms {results[name]['max_ms']:7.1f}ms"
                  f"{f'   {errors} ERRORS' if errors else ''}")
        print(json.dumps(results))


asyncio.run(main())
