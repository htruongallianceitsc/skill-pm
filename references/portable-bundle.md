# Portable Single-JSON Bundle

## Purpose

The normal workspace folder is best for developers, Git, scripts, and large content. A generic client can also operate on one portable JSON file so the project remains usable without the API or direct filesystem access.

## Bundle contract

`project.bundle.json` contains:

- `project`: canonical `project.json` content;
- `workspaceDefinition`: entity registry, relation map, lookups, quality rules, and schemas;
- `entities[]`: every canonical entity plus referenced UTF-8 file content;
- `changeManagement[]`: whitelisted audit state, ChangeSets, and named baselines;
- `bundleHash`: SHA-256 over the canonical bundle payload excluding the hash field itself.

The bundle is a transport/container format, not a second independent source of truth. When working in folder mode, canonical files remain authoritative. When working in single-file client mode, the loaded bundle is authoritative until exported/imported or synchronized.

## Export

```bash
python scripts/pm_project.py bundle-export \
  --project ./MyProject \
  --output ./project.bundle.json
```

The export embeds text content referenced by entity `content[]` entries. Binary content is intentionally excluded from standalone import in the starter implementation; keep large/binary artifacts external or extend the protocol with base64/object storage metadata.

## Import

```bash
python scripts/pm_project.py bundle-import \
  --bundle ./project.bundle.json \
  --path ./RestoredProject
```

Use `--force` only when intentionally overwriting bundle-owned files in a non-empty target folder.

## Generic client behavior

A browser/desktop client can:

1. Open one bundle JSON file.
2. Read `workspaceDefinition` to build navigation/forms/tables/relations dynamically.
3. CRUD `entities[]` in memory using the same shared envelope and schemas.
4. Preserve `uid`, `localRef`, and locked `id/code` rules.
5. Recalculate semantic `updatedAt`/`revision` on edits.
6. Preserve `changeManagement[]` so audit/review/baseline history travels with the standalone project.
7. Export the updated bundle to disk or send the same entity payloads to the sync API.

Do not store generated indexes inside the bundle; rebuild them from the entity list when needed.

## WorkPlans in the bundle

The bundle also contains `agentWorkflow[]`, which carries `.pm/workplans/PLAN-*.json`. `.pm/agent-workflow.json` and `schemas/core/workplan.schema.json` travel inside `workspaceDefinition`. Therefore a standalone browser/desktop client can review and edit plans, preserve their lifecycle/events/execution links, and later restore the same planning state back to folder mode.
