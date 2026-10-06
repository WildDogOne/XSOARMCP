"""Field projection for XSOAR's bulky list endpoints.

XSOAR returns every record in full: a single incident carries all its CustomFields, labels, raw email
headers and SLA timers, so a handful of search results can blow past an MCP client's output limit.
The API has no field selection of its own, so the tools listed in DEFAULT_FIELDS get an extra
`fields` argument and their records are trimmed server-side after the call:

- omitted: the compact default set below
- a list of names: just those (dotted paths such as "CustomFields.verdict" reach into nested objects)
- ["*"]: the full, untrimmed records

Records are found generically: a top-level JSON array, or any array of objects one level down
(`data`, `iocObjects`, `scripts`, ...). Scalars beside them, like `total`, are kept as-is.
"""

# No `from __future__ import annotations`: the `fields` annotation in _wrap() closes over a
# per-tool description, and FastMCP evaluates it at runtime to build the input schema.

import json
from collections.abc import Sequence
from typing import Annotated, Any

from fastmcp.server.transforms import GetToolNext, Transform
from fastmcp.tools.base import Tool, ToolResult
from fastmcp.tools.tool_transform import forward
from fastmcp.utilities.versions import VersionSpec
from mcp.types import TextContent
from pydantic import Field

ALL = "*"

DEFAULT_FIELDS: dict[str, list[str]] = {
    "searchIncidents": [
        "id", "name", "type", "severity", "status", "owner", "created", "occurred", "closed",
        "sourceBrand", "playbookId", "investigationId", "runStatus",
    ],
    "indicatorsSearch": [
        "id", "value", "indicator_type", "score", "firstSeen", "lastSeen", "investigationIDs",
    ],
    "searchInvestigations": ["id", "name", "type", "status", "created", "closed", "runStatus"],
    "searchEvidence": [
        "id", "incidentId", "entryId", "description", "tags", "markedBy", "markedDate", "occurred",
    ],
    "getAllReports": [
        "id", "name", "description", "type", "reportType", "recurrent", "humanCron",
        "nextScheduledTime", "latestReportTime",
    ],
    "getAllWidgets": ["id", "name", "description", "dataType", "widgetType", "category"],
    "getAutomationScripts": ["id", "name", "comment", "tags", "type", "enabled", "deprecated"],
    "getIncidentsFieldsByIncidentType": ["id", "name", "cliName", "type", "description"],
    "getAudits": ["id", "modified", "user", "type", "action", "object", "identifier"],
}


def _pick(record: dict, path: str) -> tuple[bool, Any]:
    value: Any = record
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return False, None
        value = value[part]
    return True, value


def _project_record(record: Any, fields: list[str]) -> Any:
    if not isinstance(record, dict):
        return record
    out: dict = {}
    for path in fields:
        found, value = _pick(record, path)
        if not found:
            continue
        *parents, leaf = path.split(".")
        target = out
        for part in parents:
            target = target.setdefault(part, {})
        target[leaf] = value
    return out


def _is_records(value: Any) -> bool:
    return isinstance(value, list) and any(isinstance(item, dict) for item in value)


def project(payload: Any, fields: list[str]) -> Any:
    if _is_records(payload):
        return [_project_record(r, fields) for r in payload]
    if isinstance(payload, dict):
        return {
            k: [_project_record(r, fields) for r in v] if _is_records(v) else v
            for k, v in payload.items()
        }
    return payload


def _wrap(tool: Tool, defaults: list[str]) -> Tool:
    fields_description = (
        'Fields to return per record; dotted paths reach nested fields (e.g. "CustomFields.verdict"). '
        f'Omit for the compact default ({", ".join(defaults)}); pass ["*"] for full records, which '
        "can be very large."
    )

    async def trimmed(
        fields: Annotated[list[str] | None, Field(description=fields_description)] = None,
        **kwargs: Any,
    ) -> ToolResult:
        result = await forward(**kwargs)
        if fields and ALL in fields:
            return result
        text = next((c.text for c in result.content if isinstance(c, TextContent)), None)
        try:
            payload = json.loads(text) if text is not None else None
        except json.JSONDecodeError:
            return result  # not JSON (e.g. an error message) - pass through untouched
        if payload is None:
            return result
        return ToolResult(content=json.dumps(project(payload, fields or defaults)))

    return Tool.from_tool(
        tool,
        transform_fn=trimmed,
        description=(
            f"{tool.description or ''}\n\nRecords are trimmed to a compact set of fields by default; "
            "use `fields` to choose others."
        ).strip(),
        output_schema=None,
    )


class FieldProjection(Transform):
    """Adds the `fields` argument to every tool in DEFAULT_FIELDS."""

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [_wrap(t, DEFAULT_FIELDS[t.name]) if t.name in DEFAULT_FIELDS else t for t in tools]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        tool = await call_next(name, version=version)
        if tool is None or name not in DEFAULT_FIELDS:
            return tool
        return _wrap(tool, DEFAULT_FIELDS[name])
