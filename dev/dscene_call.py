#!/usr/bin/env python3
"""Call dscene tools from the command line (an MCP client). Run in the VM:
    ~/.venvs/scenebar/bin/python dev/dscene_call.py               # list tools
    ~/.venvs/scenebar/bin/python dev/dscene_call.py get_scene
    ~/.venvs/scenebar/bin/python dev/dscene_call.py toast '{"text": "hi"}'
"""
import asyncio
import json
import sys

from mcp import Client

URL = "http://127.0.0.1:8765/mcp"


async def main():
    async with Client(URL) as c:
        if len(sys.argv) < 2:
            for t in (await c.list_tools()).tools:
                print(f"{t.name:24s} {(t.description or '').splitlines()[0][:90]}")
            return
        args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
        r = await c.call_tool(sys.argv[1], args)
        for part in r.content:
            print(part.text if part.type == "text" else f"[{part.type}: {len(part.data)} base64 chars]")
        if getattr(r, "is_error", False):
            sys.exit(1)


asyncio.run(main())
