"""Generate src/xsoar_mcp/openapi/xsoar.generated.json from Palo Alto's own XSOAR API spec.

Downloads the official Swagger 2.0 spec Palo Alto publishes in the demisto-py repo (the official
XSOAR Python SDK: https://github.com/demisto/demisto-py), converts it to OpenAPI 3.0 (FastMCP
requires 3.x; the upstream file is Swagger 2.0), and writes the result as this package's data.

The spec is already small and scoped (72 paths) - the whole thing is kept as-is, no trimming.

Requires Node/npx on PATH (only for this generation step - the server itself has no Node
dependency) to run swagger2openapi, since no actively maintained pure-Python Swagger-2-to-OpenAPI-3
converter covers this spec's edge cases (formData params, etc.) as reliably.

Re-run this whenever Palo Alto updates the upstream spec.

Usage:
    uv run python scripts/fetch_spec.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

SOURCE_URL = "https://raw.githubusercontent.com/demisto/demisto-py/master/server_api_swagger.json"
OUTPUT_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "xsoar_mcp" / "openapi" / "xsoar.generated.json"
)


def download(url: str) -> bytes:
    print(f"Downloading {url} ...", file=sys.stderr)
    with urllib.request.urlopen(url) as resp:  # noqa: S310 - trusted, hardcoded PANW URL
        return resp.read()


def convert_swagger2_to_openapi3(swagger2_bytes: bytes) -> dict:
    if shutil.which("npx") is None:
        raise SystemExit(
            "npx not found on PATH. This script needs Node/npx to run swagger2openapi "
            "(only for spec generation - the server itself has no Node dependency)."
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        src = Path(tmpdir) / "swagger2.json"
        dst = Path(tmpdir) / "openapi3.json"
        src.write_bytes(swagger2_bytes)

        print("Converting Swagger 2.0 -> OpenAPI 3.0 via swagger2openapi ...", file=sys.stderr)
        result = subprocess.run(
            ["npx", "--yes", "swagger2openapi", str(src), "-o", str(dst), "--patch"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SystemExit(
                "swagger2openapi conversion failed:\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return json.loads(dst.read_text())


def main() -> None:
    swagger2_bytes = download(SOURCE_URL)
    openapi3_spec = convert_swagger2_to_openapi3(swagger2_bytes)

    # The upstream spec has no `servers` entry (Swagger 2.0 used host/basePath instead, which
    # swagger2openapi maps to a servers[0].url of "https://hostname:443" - a placeholder, not a
    # usable default). FastMCP's default client falls back to spec.servers[0].url only when no
    # explicit client is passed; xsoar_mcp/server.py always passes one built from XSOAR_FQDN, so
    # this only matters if that ever changes - kept explicit here for clarity either way.
    openapi3_spec["servers"] = [{"url": "https://api-{fqdn}/xsoar/public/v1"}]

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(openapi3_spec, indent=2))

    n_paths = len(openapi3_spec.get("paths", {}))
    print(f"Wrote {OUTPUT_PATH} - {n_paths} paths.", file=sys.stderr)


if __name__ == "__main__":
    main()
