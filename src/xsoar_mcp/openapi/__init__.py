"""Loads the XSOAR OpenAPI spec the tools are generated from.

Two pieces are combined:

- ``xsoar.generated.json`` - the bulk of the API, generated from Palo Alto's own Swagger spec by
  ``scripts/fetch_spec.py``. Re-running that script overwrites this file.
- ``playbook_overlay.json`` - a small, hand-maintained set of operations the upstream Swagger spec
  does not carry but the XSOAR 8 *public* API (``/xsoar/public/v1``) does serve, verified against a
  live tenant: ``POST /playbook/search``, ``GET /playbook/{playbookId}`` and
  ``GET /inv-playbook/{investigationId}``. The upstream demisto-py spec predates / omits these
  playbook *read* routes (it only has the ``/playbook/save/yaml`` write and the running-task
  ``/inv-playbook/task/*`` ops), so they live in an overlay that is merged in at build time and
  therefore survives regenerating ``xsoar.generated.json``.

``load_merged`` is what the server and the dev scripts use, so the overlay operations are treated
exactly like generated ones (classified in toolsets.py, projected, schema-checked). The merge is
collision-checked: an overlay path or operationId that the generated spec already defines raises
``OverlayCollisionError`` rather than silently shadowing it.
"""

from __future__ import annotations

import json
from importlib import resources

GENERATED_FILE = "xsoar.generated.json"
OVERLAY_FILE = "playbook_overlay.json"


class OverlayCollisionError(ValueError):
    """The overlay defines a path or operationId that the generated spec already has."""

    def __init__(self, path_collisions: list[str], operation_collisions: list[str]) -> None:
        self.path_collisions = path_collisions
        self.operation_collisions = operation_collisions
        bits = []
        if path_collisions:
            bits.append(f"path(s) {path_collisions}")
        if operation_collisions:
            bits.append(f"operationId(s) {operation_collisions}")
        super().__init__("playbook_overlay collides with the generated spec: " + "; ".join(bits))


def _read(name: str) -> dict:
    return json.loads(resources.files(__package__).joinpath(name).read_text())


def load_generated() -> dict:
    """The generated spec on its own (without the overlay)."""
    return _read(GENERATED_FILE)


def load_overlay() -> dict:
    """The hand-maintained XSOAR-8-public-API-only operations."""
    return _read(OVERLAY_FILE)


def _operation_ids(paths: dict) -> set[str]:
    return {
        op["operationId"]
        for methods in paths.values()
        for op in methods.values()
        if isinstance(op, dict) and "operationId" in op
    }


def merge(spec: dict, overlay: dict) -> dict:
    """Merge ``overlay``'s paths into ``spec`` in place and return ``spec``.

    Raises ``OverlayCollisionError`` if the overlay redefines a path or an operationId the spec
    already has. The collision check runs before any mutation, so a raise leaves ``spec`` untouched.
    """
    spec_paths = spec.setdefault("paths", {})
    overlay_paths = overlay.get("paths", {})
    path_collisions = sorted(set(overlay_paths) & set(spec_paths))
    operation_collisions = sorted(_operation_ids(overlay_paths) & _operation_ids(spec_paths))
    if path_collisions or operation_collisions:
        raise OverlayCollisionError(path_collisions, operation_collisions)
    spec_paths.update(overlay_paths)
    return spec


def load_merged() -> dict:
    """The generated spec with the overlay's paths merged in - the spec the server builds from."""
    return merge(load_generated(), load_overlay())
