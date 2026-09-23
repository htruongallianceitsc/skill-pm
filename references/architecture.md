# Architecture

## Goal

Use a project folder as a portable, local-first database that an AI agent, CLI, desktop client, or filesystem-capable web client can read and edit without requiring the Project Manager API. The API adds collaboration, central viewing/editing, official identity assignment, and remote synchronization.

## Folder layout

```text
project-root/
├── project.json
├── .pm/
│   ├── config.json
│   ├── entity-types.json
│   ├── relation-map.json
│   ├── lookups.json
│   ├── quality-rules.json
│   ├── manifest.json
│   ├── sync-state.json
│   ├── change-management.json
│   ├── audit-state.json
│   ├── changesets/
│   ├── baselines/
│   ├── conflicts/
│   ├── reports/
│   │   └── quality-report.json
│   └── indexes/
│       ├── entity-index.json
│       └── relation-index.json
├── entities/
│   └── <configured-type-folders>/*.json
├── files/
│   └── <entity-type>/<uid>/...
└── schemas/
    ├── core/entity.schema.json
    └── entities/*.data.schema.json
```

The entity folders are not hard-coded. `.pm/entity-types.json` maps type names to storage, schemas, lifecycle, and UI hints.

## Canonical vs generated data

Canonical local project data:

- `project.json`;
- entity JSON files;
- optional referenced content files;
- entity registry, relation map, lookups, quality rules, change-management policy, and schemas;
- audited `.pm/changesets/*` and named `.pm/baselines/*`.

Sync/bookkeeping state:

- `.pm/sync-state.json`;
- `.pm/audit-state.json`;
- `.pm/conflicts/`.

Generated/disposable caches:

- `.pm/manifest.json`;
- `.pm/indexes/*`;
- `.pm/reports/*`.

Never treat generated indexes/reports as the canonical source for business data.

## Identity model

Every entity has:

- `uid`: client-created permanent global key; never changes;
- `id`: official server identifier; null before first server mapping;
- `code`: official server/business code; null before first server mapping;
- `localRef`: stable readable client reference such as `local:requirement:91c5a2f1`.

Relations always use `uid`. Server identity assignment therefore never requires relation rewrites.

## Local CRUD lifecycle

1. Resolve configuration for the entity type.
2. Create/edit the common envelope and type-specific `data`.
3. Validate the type's data schema and configured lifecycle status.
4. Validate dynamic relations.
5. Atomically write JSON.
6. Rebuild manifest/indexes.
7. Record the semantic edit as a ChangeSet, including referenced UTF-8 content payloads.
8. Generate quality findings when useful.
9. Sync optionally.

## Delete lifecycle

Soft-delete using `isDeleted: true` and `deletedAt`. Keep the file until remote deletion acknowledgement. Restoration clears these fields and creates another semantic revision.

## Client runtime

A standalone client can operate without API access:

1. Load project configuration.
2. Generate navigation/forms/lists from entity definitions and schemas.
3. Load or rebuild indexes.
4. Query summaries from indexes and open entity JSON on demand.
5. Validate and atomically rewrite changed JSON.
6. Rebuild derived state.
7. Synchronize when connectivity is available.

## Workspace-definition sync

A generic remote web app cannot correctly render custom entity types from entity data alone. Therefore synchronization includes a versioned workspace definition consisting of:

- entity type registry;
- relation map;
- lookups;
- quality rules;
- core/data schemas.

The workspace definition has its own payload hash and remote version in sync state. Configuration conflicts are handled separately from normal entity conflicts.

## Extensibility

Adding `release`, `decision`, `risk`, `meeting-note`, or any custom type should require configuration and schema changes only. Core CLI/client code should not contain a starter-type switch statement.

## Intelligence control plane

Keep `.pm/intelligence.json` with the portable workspace definition. It configures bounded graph profiles and diagnostics; it is synchronized and included in `project.bundle.json`. The intelligence engine consumes canonical entities plus relation/quality configuration and never replaces those sources of truth.

## Governance layer

ChangeSets preserve before/after payloads for normal CLI operations and approved proposals. Baselines preserve named whole-project payload snapshots for release/UAT comparison. They are project governance data, not domain entities, and must not participate in the business relation graph. See `change-management.md`.

## Agent planning layer

Keep `.pm/agent-workflow.json`, `schemas/core/workplan.schema.json`, and `.pm/workplans/*.json` separate from domain entities. WorkPlans capture a requested change, assumptions/risks/acceptance criteria, deterministic steps, reviewed base hashes, and execution outcome. They are synchronized and bundled so the web client can review/edit plans without inventing a domain entity type. A completed plan links to one ChangeSet for exact audit traceability. See `agent-workflow.md`.
