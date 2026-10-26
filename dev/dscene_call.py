#!/usr/bin/env python3
"""Call dscene tools from the command line (an MCP client). Run in the VM:
    python3 dev/dscene_call.py               # list tools
    python3 dev/dscene_call.py get_scene
    python3 dev/dscene_call.py toast '{"text": "hi"}'
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dscene.client import URL, connect  # noqa: E402


async def main():
    async with connect(URL) as c:
        if len(sys.argv) < 2:
            for t in (await c.list_tools()).tools:
                print(f"{t.name:24s} {(t.description or '').splitlines()[0][:90]}")
            return
        args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
        r = await c.call_tool(sys.argv[1], args)
        for part in r.content:
            print(part.text if part.type == "text" else f"[{part.type}: {len(part.data)} base64 chars]")
        if r.isError:
            sys.exit(1)


asyncio.run(main())
