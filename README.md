# XSOAR MCP

MCP server for Cortex XSOAR — generated from Palo Alto's own OpenAPI/Swagger spec via
[FastMCP](https://gofastmcp.com), rather than hand-written per endpoint.

The tool set is **generated, not hand-written**: `scripts/fetch_spec.py` downloads the official
Swagger 2.0 spec Palo Alto publishes in the
[demisto-py](https://github.com/demisto/demisto-py) repo (the official XSOAR Python SDK),
converts it to OpenAPI 3.0 (FastMCP needs 3.x), and `FastMCP.from_openapi()` turns that into 75
MCP tools. Re-running the generator picks up upstream API changes without hand-editing anything.

XSOAR's schema uses plain, MCP-safe property names throughout — no sanitizing step needed.

## Endpoint coverage and a caveat

The spec covers incidents, indicators, investigations/playbook tasks, evidence, reports,
automations/scripts, widgets, and content — 72 REST paths, 75 operations (a few paths have
multiple methods).

**Not yet empirically verified**: this spec's paths (`/incident`, `/incidents/search`, etc.) look
like the on-prem/classic XSOAR API surface. Palo Alto's XSOAR 8 / Cortex Cloud docs describe the
actual base URL as `https://api-{fqdn}/xsoar/public/v1/{endpoint_path}/` — a versioned gateway
prefix in front of what appear to be the same paths. `auth.py` already builds requests against that
prefixed base URL, so this should work as-is, but it hasn't been confirmed against a real XSOAR 8
tenant yet. If a tool 404s, check whether your tenant's actual path differs from what's in the
generated spec.

## Tool classification

XSOAR's API is mostly POST-based even for reads (e.g. `searchIncidents`, `indicatorsSearch` are
POST, not GET), so HTTP method alone can't separate read from write. Use this list to split
`permissions.allow` / `permissions.ask` in your MCP client, the same way as any other server:

**Read-only (24)** — safe to auto-allow:
```
getAutomationScripts, getEntryArtifact, downloadFile, entryExportArtifact, searchEvidence,
getIncidentAsCsv, getIncidentsFieldsByIncidentType, searchIncidents, indicatorsSearch,
getIndicatorsAsCsv, getIndicatorsAsSTIX, exportIndicatorsToStixBatch, exportIndicatorsToCsvBatch,
exportIncidentsToCsvBatch, searchInvestigations, getAllReports, getReportByID,
downloadLatestReport, getAudits, getDockerImages, GetStatsForDashboard, getStatsForWidget,
getAllWidgets, getWidget
```

**Mutating (51)** — creates/updates/deletes/imports/uploads/executes something, should prompt:
```
revokeUserAPIKey, saveOrUpdateScript, copyScript, deleteAutomationScript, importScript,
importClassifier, importDashboard, investigationAddEntryHandler, investigationAddEntriesSync,
investigationAddFormattedEntryHandler, updateEntryNote, updateEntryTagsOp, saveEvidence,
deleteEvidenceOp, createIncident, createIncidentsBatch, closeIncidentsBatch,
deleteIncidentsBatch, createIncidentJson, incidentFileUpload, importIncidentFields,
createOrUpdateIncidentType, importIncidentTypesHandler, indicatorsCreate, indicatorsEdit,
indicatorWhitelist, deleteIndicatorsBatch, createFeedIndicatorsJson, indicatorsCreateBatch,
addAdHocTask, taskAssign, completeTask, simpleCompleteTask, deleteAdHocTask, taskSetDue,
editAdHocTask, taskAddComment, taskUnComplete, completeTaskV2, submitTaskForm, importLayout,
importPlaybook, executeReport, uploadReport, importReputationHandler, createDockerImage,
integrationUpload, uploadContentPacks, saveWidget, importWidget, deleteWidget
```

## API key setup

Generate an API key in your tenant: **Settings → Integrations → API Keys**. You need:

| Value | Where it's used |
| --- | --- |
| **API Key** | `Authorization` header |
| **API Key ID** | `x-xdr-auth-id` header |
| **FQDN** | Base URL: `https://api-{fqdn}/xsoar/public/v1` |

Two key types exist: **Standard** (static, what this project supports) and **Advanced** (adds a
per-request nonce+timestamp HMAC to guard against replay). If your org requires Advanced keys,
`auth.py` needs extending to compute that signature — it currently only supports Standard.

## Setup (for developing/regenerating the spec)

```bash
uv sync
cp .env.example .env   # fill in XSOAR_FQDN / XSOAR_API_KEY / XSOAR_API_KEY_ID
uv run xsoar-mcp
```

Regenerating the spec (e.g. after Palo Alto updates the upstream API) requires Node/npx on
`PATH` (only for this step — the server itself has no Node dependency):

```bash
uv run python scripts/fetch_spec.py
```

## Installing it as a standalone command

The spec ships as package data (`src/xsoar_mcp/openapi/`), loaded via `importlib.resources`
rather than a path relative to the repo checkout, so the installed command below has no
dependency on this directory still existing:

```bash
uv tool install .          # from a checkout, or:
uv tool install git+https://github.com/WildDogOne/XSOARMCP   # directly from GitHub
```

This puts an `xsoar-mcp` executable on `~/.local/bin` (run `uv tool update-shell` once if it's
not already on your `PATH`).

**In a container**, install as `root` before switching to a non-root runtime user, and point both
the shim and the underlying venv at a directory every user can read (not `root`'s own home, which
is typically locked down):

```dockerfile
ENV UV_TOOL_BIN_DIR=/usr/local/bin
ENV UV_TOOL_DIR=/usr/local/share/uv-tools
RUN uv tool install git+https://github.com/WildDogOne/XSOARMCP

USER <your-runtime-user>
```

## Wiring into Claude Code

This runs over **stdio**. MCP server subprocesses do **not** inherit Claude Code's environment
automatically — `XSOAR_FQDN`/`XSOAR_API_KEY`/`XSOAR_API_KEY_ID` have to be passed explicitly via
`-e`/`--env`, even if they're already set wherever Claude Code itself runs.

`claude mcp add`'s positional arguments (`<name>` then `<commandOrUrl>`) must come **before** any
`-e`/`--env` flags — placing them after breaks the parser:

```bash
claude mcp add --scope user xsoar xsoar-mcp \
  -e XSOAR_FQDN=<your-tenant-fqdn> \
  -e XSOAR_API_KEY=<your-api-key> \
  -e XSOAR_API_KEY_ID=<your-api-key-id>
```

No `--directory` and no path to this repo anywhere in that command — it only works once
`xsoar-mcp` is installed and on `PATH` per the previous section.

If you'd rather the literal key not sit in your shell history: export the three `XSOAR_*`
variables in your own shell/secret manager first, then hand-edit `.mcp.json` yourself with
`"env": {"XSOAR_API_KEY": "${XSOAR_API_KEY}", ...}` — Claude Code expands `${VAR}` from your
environment at startup, so the secret itself never needs to appear in any file or command.

Then add the read-only tools from the **Tool classification** section above to
`permissions.allow` and the mutating ones to `permissions.ask` (both prefixed `mcp__xsoar__`).
