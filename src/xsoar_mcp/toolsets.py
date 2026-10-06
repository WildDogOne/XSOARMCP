"""Read/write classification and toolset grouping for every operation in the XSOAR spec.

XSOAR's API is mostly POST-based even for reads (e.g. searchIncidents, indicatorsSearch are POST,
not GET), and the upstream spec carries no OpenAPI tags, so neither HTTP method nor the spec itself
can tell us which operations are safe or how they group. This module is the hand-maintained source
of truth for both. If it falls out of sync with the spec, the server still starts: it logs a
warning, and treats any unclassified operation as a write tool that is only served when all
toolsets are enabled and read-only mode is off. The dev scripts (fetch_spec.py,
check_schema_safety.py) report the drift so it gets fixed before release.

Two env vars act on it, both enforced by never registering the excluded tools (so they can't be
called by name either, not just hidden from the listing):

- XSOAR_TOOLSETS: comma-separated toolset names, or "all" (the default).
- XSOAR_READ_ONLY: "true" drops every write tool, regardless of toolset.
"""

from __future__ import annotations

import os

TOOLSETS: dict[str, dict[str, list[str]]] = {
    "incidents": {
        "read": [
            "searchIncidents",
            "getIncidentAsCsv",
            "getIncidentsFieldsByIncidentType",
            "exportIncidentsToCsvBatch",
        ],
        "write": [
            "createIncident",
            "createIncidentsBatch",
            "createIncidentJson",
            "closeIncidentsBatch",
            "deleteIncidentsBatch",
            "incidentFileUpload",
        ],
    },
    "indicators": {
        "read": [
            "indicatorsSearch",
            "getIndicatorsAsCsv",
            "getIndicatorsAsSTIX",
            "exportIndicatorsToCsvBatch",
            "exportIndicatorsToStixBatch",
        ],
        "write": [
            "indicatorsCreate",
            "indicatorsCreateBatch",
            "indicatorsEdit",
            "indicatorWhitelist",
            "deleteIndicatorsBatch",
            "createFeedIndicatorsJson",
        ],
    },
    # War room entries, evidence, and playbook tasks within an investigation.
    "investigations": {
        "read": [
            "searchInvestigations",
            "getEntryArtifact",
            "downloadFile",
            "entryExportArtifact",
            "searchEvidence",
        ],
        "write": [
            "investigationAddEntryHandler",
            "investigationAddEntriesSync",  # executes a war room command
            "investigationAddFormattedEntryHandler",
            "updateEntryNote",
            "updateEntryTagsOp",
            "saveEvidence",
            "deleteEvidenceOp",
            "addAdHocTask",
            "editAdHocTask",
            "deleteAdHocTask",
            "taskAssign",
            "taskSetDue",
            "taskAddComment",
            "completeTask",
            "completeTaskV2",
            "simpleCompleteTask",
            "taskUnComplete",
            "submitTaskForm",
        ],
    },
    "reports": {
        "read": [
            "getAllReports",
            "getReportByID",
            "downloadLatestReport",
            "getAllWidgets",
            "getWidget",
            "getStatsForWidget",
            "GetStatsForDashboard",
        ],
        "write": [
            "executeReport",
            "uploadReport",
            "saveWidget",
            "importWidget",
            "deleteWidget",
            "importDashboard",
        ],
    },
    # Automations, playbooks, layouts, types/fields, integrations, docker images.
    "content": {
        "read": [
            "getAutomationScripts",
            "getDockerImages",
        ],
        "write": [
            "saveOrUpdateScript",
            "copyScript",
            "deleteAutomationScript",
            "importScript",
            "importPlaybook",
            "importLayout",
            "importClassifier",
            "importIncidentFields",
            "createOrUpdateIncidentType",
            "importIncidentTypesHandler",
            "importReputationHandler",
            "createDockerImage",
            "integrationUpload",
            "uploadContentPacks",
        ],
    },
    "admin": {
        "read": [
            "getAudits",
        ],
        "write": [
            "revokeUserAPIKey",
        ],
    },
}

# operationId -> (toolset, is_read_only)
CLASSIFICATION: dict[str, tuple[str, bool]] = {
    op_id: (toolset, access == "read")
    for toolset, groups in TOOLSETS.items()
    for access, op_ids in groups.items()
    for op_id in op_ids
}

_TRUTHY = {"1", "true", "yes", "on"}


def classification_drift(spec: dict) -> tuple[list[str], list[str]]:
    """Return (operationIds missing from CLASSIFICATION, CLASSIFICATION entries not in the spec)."""
    spec_ids = {
        op["operationId"]
        for methods in spec.get("paths", {}).values()
        for op in methods.values()
        if isinstance(op, dict) and "operationId" in op
    }
    return sorted(spec_ids - CLASSIFICATION.keys()), sorted(CLASSIFICATION.keys() - spec_ids)


def describe_drift(unclassified: list[str], stale: list[str]) -> str:
    return (
        "src/xsoar_mcp/toolsets.py is out of sync with the OpenAPI spec. "
        f"Unclassified operations (treated as write tools): {unclassified or 'none'}. "
        f"Classified but missing from spec: {stale or 'none'}."
    )


def read_only_mode() -> bool:
    return os.environ.get("XSOAR_READ_ONLY", "").strip().lower() in _TRUTHY


def enabled_toolsets() -> set[str]:
    raw = os.environ.get("XSOAR_TOOLSETS", "all").strip().lower()
    if raw in ("", "all"):
        return set(TOOLSETS)
    requested = {name.strip() for name in raw.split(",") if name.strip()}
    unknown = requested - TOOLSETS.keys()
    if unknown:
        raise RuntimeError(
            f"XSOAR_TOOLSETS contains unknown toolset(s) {sorted(unknown)} - "
            f"valid names are {sorted(TOOLSETS)} or 'all'."
        )
    return requested
