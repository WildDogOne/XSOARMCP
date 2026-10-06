"""Generate src/xsoar_mcp/openapi/xsoar.generated.json from Palo Alto's own XSOAR API spec.

Downloads the official Swagger 2.0 spec Palo Alto publishes in the demisto-py repo (the official
XSOAR Python SDK: https://github.com/demisto/demisto-py), converts it to OpenAPI 3.0 (FastMCP
requires 3.x; the upstream file is Swagger 2.0), and writes the result as this package's data.

The spec is already small and scoped (72 paths) - the whole thing is kept as-is, no trimming.

Requires Node/npx on PATH (only for this generation step - the server itself has no Node
dependency) to run swagger2openapi, since no actively maintained pure-Python Swagger-2-to-OpenAPI-3
converter covers this spec's edge cases (formData params, etc.) as reliably.

Re-run this whenever Palo Alto updates the upstream spec. It reports which operations changed
against the currently shipped spec and whether src/xsoar_mcp/toolsets.py still covers them.
Exit code is 1 whenever there's
something to do (toolsets.py needs edits, or with --check, the upstream spec has changed).

Usage:
    uv run python scripts/fetch_spec.py           # regenerate the spec and check toolsets.py
    uv run python scripts/fetch_spec.py --check   # only report; don't write anything
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from xsoar_mcp.toolsets import classification_drift, describe_drift

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


def operations(spec: dict) -> dict[str, tuple[str, str]]:
    """operationId -> (METHOD, path)"""
    return {
        op["operationId"]: (method.upper(), path)
        for path, methods in spec.get("paths", {}).items()
        for method, op in methods.items()
        if isinstance(op, dict) and "operationId" in op
    }


def report_changes(old: dict, new: dict) -> bool:
    """Print what changed between the shipped and the upstream spec; return whether anything did."""
    old_ops, new_ops = operations(old), operations(new)
    added = sorted(new_ops.keys() - old_ops.keys())
    removed = sorted(old_ops.keys() - new_ops.keys())
    moved = sorted(k for k in old_ops.keys() & new_ops.keys() if old_ops[k] != new_ops[k])

    for label, names, ops in (("Added", added, new_ops), ("Removed", removed, old_ops)):
        for name in names:
            print(f"  {label}: {name} ({' '.join(ops[name])})", file=sys.stderr)
    for name in moved:
        print(
            f"  Moved: {name} ({' '.join(old_ops[name])} -> {' '.join(new_ops[name])})",
            file=sys.stderr,
        )

    if added or removed or moved:
        return True
    if old != new:
        print("  Same operations; schemas or descriptions changed.", file=sys.stderr)
        return True
    print("  No changes against the shipped spec.", file=sys.stderr)
    return False


def main() -> None:
    check_only = "--check" in sys.argv[1:]

    swagger2_bytes = download(SOURCE_URL)
    openapi3_spec = convert_swagger2_to_openapi3(swagger2_bytes)

    # The upstream spec has no `servers` entry (Swagger 2.0 used host/basePath instead, which
    # swagger2openapi maps to a servers[0].url of "https://hostname:443" - a placeholder, not a
    # usable default). FastMCP's default client falls back to spec.servers[0].url only when no
    # explicit client is passed; xsoar_mcp/server.py always passes one built from XSOAR_FQDN, so
    # this only matters if that ever changes - kept explicit here for clarity either way.
    openapi3_spec["servers"] = [{"url": "https://api-{fqdn}/xsoar/public/v1"}]

    print("\nChanges against the shipped spec:", file=sys.stderr)
    old_spec = json.loads(OUTPUT_PATH.read_text()) if OUTPUT_PATH.exists() else {}
    changed = report_changes(old_spec, openapi3_spec)

    if not check_only and changed:
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(json.dumps(openapi3_spec, indent=2))
        n_paths = len(openapi3_spec.get("paths", {}))
        print(f"\nWrote {OUTPUT_PATH} - {n_paths} paths.", file=sys.stderr)

    print("\ntoolsets.py:", file=sys.stderr)
    unclassified, stale = classification_drift(openapi3_spec)
    if unclassified or stale:
        print(f"  {describe_drift(unclassified, stale)}", file=sys.stderr)
        if unclassified:
            new_ops = operations(openapi3_spec)
            print("  Classify these in toolsets.py:", file=sys.stderr)
            for op_id in unclassified:
                print(f"    {op_id} ({' '.join(new_ops[op_id])})", file=sys.stderr)
        if stale:
            print("  Remove the stale entries from toolsets.py.", file=sys.stderr)
    else:
        print("  All operations classified.", file=sys.stderr)

    if changed and not (unclassified or stale):
        print(
            "\nNext: update the README's toolset table and permission lists if needed, then "
            "reinstall with `uv tool install --reinstall .`."
            if not check_only
            else "\nUpstream changed - rerun without --check to update the shipped spec.",
            file=sys.stderr,
        )

    if unclassified or stale or (check_only and changed):
        sys.exit(1)


if __name__ == "__main__":
    main()
