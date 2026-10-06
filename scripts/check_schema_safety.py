"""Dev helper: verify every generated tool's schema only uses MCP-safe property names, and that
every spec operation is classified in toolsets.py."""
import asyncio
import json
import re
import sys

from xsoar_mcp.server import SPEC_RESOURCE, build_server
from xsoar_mcp.toolsets import classification_drift, describe_drift

SAFE = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")


async def main() -> None:
    server = build_server()
    tools = await server.list_tools()
    all_ok = True
    for tool in tools:
        props = (tool.parameters or {}).get("properties", {})
        bad = [k for k in props if not SAFE.match(k)]
        status = "OK" if not bad else f"BAD: {bad}"
        if bad:
            all_ok = False
        print(f"{tool.name}: {len(props)} props - {status}")
    print()
    print("ALL SAFE" if all_ok else "SOME TOOLS STILL HAVE INVALID PROPERTY NAMES")

    unclassified, stale = classification_drift(json.loads(SPEC_RESOURCE.read_text()))
    if unclassified or stale:
        print(describe_drift(unclassified, stale))
    else:
        print("ALL OPERATIONS CLASSIFIED")

    if not all_ok or unclassified or stale:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
