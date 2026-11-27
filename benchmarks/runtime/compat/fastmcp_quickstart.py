"""
A FastMCP example server for the compatibility matrix (TEST-ONLY): the quickstart server of the
MCP Python SDK's README (mcp 1.x, ``mcp.server.fastmcp``), transcribed: one tool, one resource.
Run with the pinned SDK's interpreter (``/tmp/sgpy``: mcp 1.30.0) over stdio.
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Demo")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers"""
    return a + b


@mcp.resource("greeting://{name}")
def get_greeting(name: str) -> str:
    """Get a personalized greeting"""
    return f"Hello, {name}!"


if __name__ == "__main__":
    mcp.run()
