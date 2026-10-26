"""Connect to a dscene MCP server (e.g. the one inside SceneBar) as an MCP client:

    async with client.connect() as dscene:
        tools = (await dscene.list_tools()).tools
        result = await dscene.call_tool("get_scene", {})

Written for the MCP Python SDK 1.x (Arch's python-mcp).
"""
import contextlib

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = "http://127.0.0.1:8765/mcp"


@contextlib.asynccontextmanager
async def connect(url=URL):
    """An initialised MCP session with the dscene server at `url`."""
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session
