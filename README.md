# XSOAR MCP

MCP server for Cortex XSOAR — generated from Palo Alto's own OpenAPI/Swagger spec via
[FastMCP](https://gofastmcp.com), rather than hand-written per endpoint.

The tool set is **generated, not hand-written**: `scripts/fetch_spec.py` downloads the official
Swagger 2.0 spec Palo Alto publishes in the
[demisto-py](https://github.com/demisto/demisto-py) repo (the official XSOAR Python SDK),
converts it to OpenAPI 3.0 (FastMCP needs 3.x), and `FastMCP.from_openapi()` turns that — merged
with a small hand-maintained overlay of XSOAR 8 public-API-only routes the upstream spec omits
(see [Playbook routes](#playbook-routes-added-via-overlay)) — into 78 operations, 76 of which
become MCP tools (two routes the public API does not serve are disabled, see below). Re-running
the generator picks up upstream API changes without dropping the overlay.

XSOAR's schema uses plain, MCP-safe property names throughout — no sanitizing step needed.

## Endpoint coverage and a caveat

The spec covers incidents, indicators, investigations/playbook tasks, evidence, reports,
automations/scripts, widgets, and content — 75 REST paths, 78 operations (a few paths have
multiple methods). 72 paths / 75 operations come from the generated spec; the other 3 paths
(`POST /playbook/search`, `GET /playbook/{playbookId}`, `GET /inv-playbook/{investigationId}`) come
from the overlay described under [Playbook routes](#playbook-routes-added-via-overlay).

**Verified against a live XSOAR 8 hosted tenant (2026-11-24)**: the spec's classic paths
(`/automation/search`, etc.) do work behind the versioned gateway prefix
`https://api-{fqdn}/xsoar/public/v1/{endpoint_path}` that `auth.py` builds — those calls return
`200` with the Advanced Cortex API key. Two different kinds of path are *not* served by the public
gateway and answer `HTTP 303` rather than `404`: routes that **are** in the bundled spec but the
gateway doesn't serve (e.g. `searchInvestigations` — tracked in `UNAVAILABLE_ON_PUBLIC_API`, see the
next section), and private/UI-only endpoints that are **not** spec routes at all (e.g. the tenant
front-end's `/automation/playbook/*` playbook search — there is no tool for those, and they were
never candidates for `UNAVAILABLE_ON_PUBLIC_API`). If a tool 404s on your tenant, check whether its
path differs from what's in the generated spec.

## Routes not served by the public API

The bundled spec is the full XSOAR API; the public gateway (`/xsoar/public/v1`, what an API key
can reach) serves only part of it. Routes it does not serve do not 404 — the tenant answers with
`HTTP 303` redirecting to `/#/404` with an HTML body. Verified against a live tenant (2026-11-24),
these operations are disabled and never registered as tools (they neither appear in the tool list
nor answer to name); see `UNAVAILABLE_ON_PUBLIC_API` in `src/xsoar_mcp/toolsets.py`:

| Operation | Route | Why removed |
| --- | --- | --- |
| `searchInvestigations` | `POST /investigations/search` | 303 on the public API; investigation/war-room search is private-API only. `searchIncidents` covers incidents, `searchEvidence` covers evidence. |
| `getIncidentsFieldsByIncidentType` | `GET /incidentfields/associatedTypes/{type}` | 303 on the public API; no public equivalent for listing an incident type's fields. (It also advertised the wrong argument live: an `id` where the route requires `type`.) |

`GET /incident/fields` 303s as well but is not in the spec, so no tool existed for it. The HTTP
client (`XsoarApiClient` in `auth.py`) never follows redirects: any 3xx raises
`PublicApiRouteUnavailableError` with the status, path, and a hint that the route is not
available on the public API, instead of surfacing a raw redirect or HTML.

XSOAR also answers any POST without `Content-Type: application/json` with
`415 unsupported_media_type` — including its read-style search endpoints, and including POSTs
with an empty body. FastMCP's OpenAPI provider sends tools called without body arguments (e.g.
`getAutomationScripts` with no filters) as bodyless POSTs, so `XsoarApiClient.build_request`
gives every bodyless POST/PUT/PATCH a `json={}` body, which httpx sends as
`Content-Type: application/json` + `{}`. Bodies that already carry a Content-Type (JSON search
filters, multipart uploads) are left untouched.

## Playbook routes (added via overlay)

The upstream demisto-py Swagger spec carries **no playbook *read* route** — only the
`/playbook/save/yaml` write (`importPlaybook`) and the running-task `/inv-playbook/task/*` ops. So
the generated spec has no way to list playbooks, read a playbook's definition, or read the state of a
playbook run, even though the XSOAR 8 *public* API does serve all three.

Verified against a live XSOAR 8 hosted tenant (2026-11-24) with an Advanced Cortex API key:

| Route | Result with an API key |
| --- | --- |
| `POST /xsoar/public/v1/playbook/search` | **200** — `{playbooks: [...], tags: [...], total: N}`; body `{"query": "<name filter>", "size": N, "page": N}` (the API honours `size`/`page`; an empty query returns every playbook, each carrying its full task graph) |
| `GET /xsoar/public/v1/playbook/{playbookId}` | **200** — the playbook definition (`inputs`, `outputs`, full `tasks` graph, tens–hundreds of KB); `playbookId` is the `id` from search (sometimes the display name, sometimes a UUID) |
| `GET /xsoar/public/v1/inv-playbook/{investigationId}` | **200** — the live **execution state** of the playbook run for that investigation: run metadata (`id`, `playbookId`, `state`, `startTaskId`, `startDate`, `modified`, `netDurationMs`, `runIndex`, `pbHistory`, `view`, `Dirty`) plus `tasks{}` keyed by task id, each entry carrying `state` (`Completed` / `WillNotBeExecuted` / `Error` / …), timings, `completedBy`, `retryCount`, `nextTasks` and the full task definition. ~200 KB untrimmed for an 11-task run. |
| `POST /xsoar/public/v1/automation/playbook/search` | `303` — this is the **private/UI** route, not callable with an API key |

Of the `/inv-playbook/*` family only `GET /inv-playbook/{investigationId}` is served publicly — the
other read variants (task lists, context per run) answer `404`/`303`. The task-*mutating* ops the
generated spec does carry (`/inv-playbook/task/complete`, `/task/assign`, …) are unaffected.

These three public routes are added in `src/xsoar_mcp/openapi/playbook_overlay.json`, merged into the
generated spec at build time by `xsoar_mcp/openapi/__init__.py:load_merged()`, so they survive
`fetch_spec.py` regenerating `xsoar.generated.json`. They are exposed as the read tools
**`searchPlaybooks`** and **`getPlaybookById`** (toolset `content`) and **`getPlaybookExecution`**
(toolset `investigations`). `searchPlaybooks` is projected down to a compact field set because each
playbook object carries its whole task graph; `getPlaybookExecution` is projected in two scopes —
bare `fields` names select the run's top level, `tasks.`-prefixed names each task entry (e.g.
`fields=["tasks.state", "tasks.task.name"]` answers "what is still running" in a few hundred bytes).

**Monitoring a run:** `getPlaybookExecution` takes the investigation/incident id (the `id` or
`investigationId` from `searchIncidents`) and shows per-task progress for a running or finished run,
including runs started from the tenant UI in **test mode**.

**Caveat — no raw YAML.** The public API returns the *parsed* playbook definition (the task graph),
not the playbook's YAML source. There is no public route that hands back the YAML text with an API
key (the UI gets it from private routes). For reviewing what a playbook does, the task graph is
complete; for the literal YAML, export from the tenant UI.

## Toolsets and read-only mode

XSOAR's API is mostly POST-based even for reads (e.g. `searchIncidents`, `indicatorsSearch` are
POST, not GET), and the upstream spec has no tags, so every operation is classified by hand in
[`src/xsoar_mcp/toolsets.py`](src/xsoar_mcp/toolsets.py) as read or write and grouped into a
toolset. If that file and the spec disagree, the server still starts but logs a warning, and
any operation missing from it is treated as a write tool: served only when all toolsets are
enabled, dropped in read-only mode.

Each tool carries the MCP `readOnlyHint` annotation plus tags for its toolset and `read`/`write`.
Two env vars filter the tool set. Filtered tools are never registered, so they can't be called by
name either:

| Env var | Default | Effect |
| --- | --- | --- |
| `XSOAR_TOOLSETS` | `all` | Comma-separated toolsets to enable, e.g. `incidents,indicators` |
| `XSOAR_READ_ONLY` | `false` | `true` drops every write tool |

| Toolset | Read | Write | Covers |
| --- | --- | --- | --- |
| `incidents` | 3 | 6 | Incident search, create, close, delete, export |
| `indicators` | 5 | 6 | Indicator search, create, edit, whitelist, export |
| `investigations` | 5 | 18 | War room entries, evidence, playbook runs/tasks |
| `reports` | 7 | 6 | Reports, widgets, dashboard statistics |
| `content` | 4 | 14 | Automations, playbooks (search + get definition), layouts, types/fields, integrations, docker images |
| `admin` | 1 | 1 | Audit log, API key revocation |

`uv run python scripts/list_tools.py` prints exactly which tools a given combination leaves
enabled, with their tags.

### Client permission rules

Read-only mode is the simplest guard. For finer control, split `permissions.allow` /
`permissions.ask` in your MCP client with these lists (they mirror `toolsets.py`):

**Read-only (25)** — safe to auto-allow:
```
getAutomationScripts, getEntryArtifact, downloadFile, entryExportArtifact, searchEvidence,
getIncidentAsCsv, searchIncidents, indicatorsSearch,
getIndicatorsAsCsv, getIndicatorsAsSTIX, exportIndicatorsToStixBatch, exportIndicatorsToCsvBatch,
exportIncidentsToCsvBatch, getAllReports, getReportByID,
downloadLatestReport, getAudits, getDockerImages, GetStatsForDashboard, getStatsForWidget,
getAllWidgets, getWidget, searchPlaybooks, getPlaybookById, getPlaybookExecution
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

| Value | Env var | Where it's used |
| --- | --- | --- |
| **API Key** | `XSOAR_API_KEY` | `Authorization` header (or the signature input, for Advanced) |
| **API Key ID** | `XSOAR_API_KEY_ID` | `x-xdr-auth-id` header |
| **FQDN** | `XSOAR_FQDN` | Base URL: `https://api-{fqdn}/xsoar/public/v1` (a full URL also works — `auth.py` normalizes either form) |

Two key types exist, picked when you generate the key: **Standard** (static header, the raw key)
and **Advanced** (guards against replay — every request carries a fresh nonce + timestamp, and
`Authorization` is `sha256(api_key + nonce + timestamp)` instead of the raw key). Both are
supported; set `XSOAR_API_KEY_TYPE=standard` or `advanced` to match what you generated (defaults
to `advanced`). Getting this wrong produces a `401 Unauthorized` with no further detail from the
server, since a validly-formed request with the wrong signing scheme just fails auth like any
other bad credential.

## Setup (for developing/regenerating the spec)

```bash
uv sync
cp .env.example .env   # fill in XSOAR_FQDN / XSOAR_API_KEY / XSOAR_API_KEY_ID
uv run xsoar-mcp
```

Regenerating the spec (e.g. after Palo Alto updates the upstream API) requires Node/npx on
`PATH` (only for this step — the server itself has no Node dependency):

```bash
uv run python scripts/fetch_spec.py --check   # is there an upstream update? writes nothing
uv run python scripts/fetch_spec.py           # pull it into the package
```

Both report which operations upstream added, removed, or moved, and whether
`src/xsoar_mcp/toolsets.py` still classifies every operation, listing any that aren't with their
method and path. The exit code is 1 whenever something needs doing. Once `toolsets.py` is updated, rerun the script
(or `scripts/check_schema_safety.py`) until it's clean, then `uv tool install --reinstall .`.

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
  -e XSOAR_API_KEY_ID=<your-api-key-id> \
  -e XSOAR_API_KEY_TYPE=<standard-or-advanced>
```

No `--directory` and no path to this repo anywhere in that command — it only works once
`xsoar-mcp` is installed and on `PATH` per the previous section.

If you'd rather the literal key not sit in your shell history: export the three `XSOAR_*`
variables in your own shell/secret manager first, then hand-edit `.mcp.json` yourself with
`"env": {"XSOAR_API_KEY": "${XSOAR_API_KEY}", ...}` — Claude Code expands `${VAR}` from your
environment at startup, so the secret itself never needs to appear in any file or command.

Then add the read-only tools from the **Client permission rules** section above to
`permissions.allow` and the mutating ones to `permissions.ask` (both prefixed `mcp__xsoar__`).
