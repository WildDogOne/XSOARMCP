"""Dev helper: build the server and print the tools it exposes, without calling XSOAR.

Respects XSOAR_TOOLSETS / XSOAR_READ_ONLY, so it also shows what a given filter leaves enabled.

Usage:
    XSOAR_FQDN=example.paloaltonetworks.com XSOAR_API_KEY=dummy XSOAR_API_KEY_ID=dummy \
    uv run python scripts/list_tools.py
"""

import asyncio

from xsoar_mcp.server import build_server


async def main() -> None:
    server = build_server()
    tools = await server.list_tools()
    print(f"Total tools: {len(tools)}\n")
    for tool in tools:
        print(f"--- {tool.name} [{', '.join(sorted(tool.tags))}] ---")
        print((tool.description or "")[:150])
        print()


if __name__ == "__main__":
    asyncio.run(main())
