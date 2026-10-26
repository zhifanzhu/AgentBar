#!/usr/bin/env python3
"""Time one agent-style Gemini call (all dscene tools + a screenshot) on several models.
Run in the VM while the bar is up:  python3 dev/latency_bench.py [image.jpg]
Tool calls are not executed; only the model's response time is measured."""
import asyncio
import base64
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brain  # noqa: E402

CANDIDATES = [("gemini-3.8-flash", "low"), ("gemini-3.6-flash", "minimal"), ("gemini-3.5-flash-lite", "minimal")]
TRIALS = 3


async def tool_decls():
    from dscene.client import connect
    async with connect(brain.DSCENE_URL) as c:
        return [{"type": "function", "name": t.name, "description": t.description or "",
                 "parameters": brain.plain_schema(t.inputSchema)} for t in (await c.list_tools()).tools]


def main():
    cfg = brain.load_config()
    key = brain.gemini_key(cfg)
    tools = asyncio.run(tool_decls()) + [{"type": "google_search"}]
    content = [{"type": "text", "text": "This is the user's screen with Blender open. Guide them to add "
                                        "a bevel modifier to the cube: do the first step now."}]
    if len(sys.argv) > 1:
        content.append({"type": "image", "mime_type": "image/jpeg",
                        "data": base64.b64encode(Path(sys.argv[1]).read_bytes()).decode()})
    for model, level in CANDIDATES:
        times, first = [], None
        for _ in range(TRIALS):
            t0 = time.perf_counter()
            try:
                resp = brain._http(f"{brain.GEMINI}/interactions", headers={"x-goog-api-key": key}, body={
                    "model": model, "input": content, "system_instruction": brain.SYSTEM, "tools": tools,
                    "generation_config": {"thinking_level": level}, "store": False})
            except brain.BrainError as e:
                print(f"{model:24s} {level:8s} ERROR {e}")
                break
            times.append(time.perf_counter() - t0)
            calls = [s.get("name") for s in resp.get("steps", []) if s.get("type") == "function_call"]
            first = first or (calls or ["(text)"])
        if times:
            print(f"{model:24s} {level:8s} median {statistics.median(times):4.1f}s  "
                  f"(runs: {', '.join(f'{t:.1f}' for t in times)})  first action: {first}")


if __name__ == "__main__":
    main()
