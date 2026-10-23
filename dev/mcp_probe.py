"""Probe: MCP 2.x server in a background thread + high-level Client over HTTP."""
import asyncio, threading, time
from mcp import Client
from mcp.server.mcpserver import MCPServer

server = MCPServer("probe")

@server.tool()
async def add(a: int, b: int) -> dict:
    """Add two numbers."""
    return {"sum": a + b}

threading.Thread(target=lambda: asyncio.run(server.run_streamable_http_async(host="127.0.0.1", port=8799)),
                 daemon=True).start()
time.sleep(1.5)

async def main():
    async with Client("http://127.0.0.1:8799/mcp") as c:
        tools = await c.list_tools()
        t = tools.tools[0]
        print("tool:", t.name, "| schema:", t.input_schema if hasattr(t, "input_schema") else t.inputSchema)
        r = await c.call_tool("add", {"a": 2, "b": 3})
        print("result type:", type(r).__name__, "| content:", r.content, "| structured:", getattr(r, "structured_content", None) or getattr(r, "structuredContent", None))
asyncio.run(main())
