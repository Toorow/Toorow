"""Image-local smoke for the delivered MCP App resource and render declaration."""

from __future__ import annotations

import asyncio

from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport

from core import analyze_render_mcp, render_app_payload, visualization_runtime_resource
from core.mcp_profiles import RENDER_TOOL_NAME
from core.visualization_runtime_resource import VISUALIZATION_RUNTIME_URI, runtime_bundle_path


async def _smoke() -> None:
    bundle = runtime_bundle_path()
    manifest = render_app_payload.load_runtime_manifest(bundle)
    target = FastMCP("mcp-app-image-smoke")
    visualization_runtime_resource.register(target)
    analyze_render_mcp.register(target)
    async with Client(FastMCPTransport(target)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        contents = await client.read_resource(VISUALIZATION_RUNTIME_URI)
    render = tools.get(RENDER_TOOL_NAME)
    if render is None:
        raise RuntimeError("render_analyze_result is absent from the image catalog")
    uri = (render.meta or {}).get("ui", {}).get("resourceUri")
    if uri != VISUALIZATION_RUNTIME_URI:
        raise RuntimeError("render_analyze_result advertises the wrong resource")
    if not contents or "id=\"root\"" not in contents[0].text:
        raise RuntimeError("the advertised MCP App resource is not readable")
    print(
        "mcp-app smoke passed: "
        f"{bundle.stat().st_size} bytes, {len(manifest['renderers'])} renderers"
    )


def main() -> None:
    asyncio.run(_smoke())


if __name__ == "__main__":
    main()
