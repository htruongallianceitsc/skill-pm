# Project Manager API Reference

## Endpoint

Use one configurable endpoint for all synchronization:

```text
POST /api/project-manager/sync
```

The server may store entities in relational tables, document storage, or another representation. The wire contract uses `uid` as the permanent correlation key.

## Authentication

The CLI reads the bearer token from the environment variable configured by `sync.tokenEnv` (default `PM_API_TOKEN`). Do not store secrets in the project folder.

## Scope

The same endpoint supports:

- full-project sync;
- one or more entity types;
- one or more entity UIDs;
- push, pull, or bidirectional direction;
- normal optimistic concurrency;
- explicit force-local conflict resolution;
- workspace-definition sync;
- ChangeSet and release-baseline sync through the same request.

## Server responsibilities

1. Resolve project by permanent project `uid`.
2. Assign official project/entity `id` and `code` when needed.
3. Never require a client-generated official code.
4. Use `uid` for idempotent upsert correlation.
5. Maintain a monotonic remote version or ETag-equivalent for optimistic concurrency.
6. Compare `baseRemoteVersion` (preferred) or exact base remote timestamp.
7. Return conflicts instead of silently overwriting concurrent edits.
8. Validate conflict tokens for force-local operations.
9. Return a cursor covering remote changes since the previous sync.
10. Store the workspace definition so a generic web client can render project entities without hard-coded type knowledge.
11. Deduplicate audit ChangeSets by `changeSetId` and baselines by `baselineId`; acknowledge the exact accepted payload hash.
12. Expose ChangeSet/baseline history to the web client without converting governance records into domain entities.

## Entity upsert identity

On first sync a local entity may contain:

```json
{
  "uid": "client-permanent-uuid",
  "id": null,
  "code": null
}
```

The server returns official values:

```json
{
  "uid": "client-permanent-uuid",
  "id": "18422",
  "code": "REQ-00173",
  "remoteVersion": 1
}
```

After the client records this identity in sync state, later server responses must not change it. Identity migration must be an explicit administrative process, not normal sync behavior.

## Workspace definition

The request can include a changed `workspaceDefinition` containing JSON files that describe entity types, schemas, relation rules, lookups, and quality rules. The server stores this definition with its own remote version.

The server should return `definitionApplied` after accepting the local definition and `remoteDefinition` when the server has a newer definition for the client.

Treat definition conflicts with the same optimistic-concurrency principles as entity conflicts.

## Error behavior

Prefer per-item errors when one entity fails but the request itself is valid. Use HTTP 4xx/5xx for request/auth/server failures. Never acknowledge a failed entity as applied.

Recommended error fields:

```json
{
  "uid": "...",
  "code": "VALIDATION_ERROR",
  "message": "Requirement priority is invalid",
  "field": "data.priority"
}
```

## Idempotency

A retry of the same entity `uid` + unchanged payload hash should not create duplicates. A retry of a completed sync request may return the same logical result safely.

## Governance payload

The request may include:

```json
{
  "audit": {
    "changeSets": [{"changeSetId":"CHG-...","payloadHash":"sha256:...","document":{}}],
    "baselines": [{"baselineId":"BASE-...","name":"release-1.2","payloadHash":"sha256:...","document":{}}]
  }
}
```

Acknowledge accepted items explicitly so the client can persist `lastSyncedHash` in `.pm/sync-state.json`:

```json
{
  "auditApplied": [{"changeSetId":"CHG-...","remoteId":"101","remoteVersion":2,"payloadHash":"sha256:..."}],
  "baselinesApplied": [{"baselineId":"BASE-...","remoteId":"31","remoteVersion":1,"payloadHash":"sha256:..."}]
}
```

If the server does not support governance storage yet, it may ignore the optional `audit` section; the client will continue to consider those records unsynced and resend them on future syncs.

## Planning payload

WorkPlans remain governance/planning records rather than domain entities. The same sync endpoint may receive:

```json
{
  "scope": {"workPlanIds": ["PLAN-..."]},
  "planning": {
    "workPlans": [
      {
        "planId": "PLAN-...",
        "baseRemoteVersion": 2,
        "baseRemoteUpdatedAt": "2026-09-23T00:00:00Z",
        "payloadHash": "sha256:...",
        "conflictToken": null,
        "document": {}
      }
    ]
  }
}
```

Recommended response fields:

```json
{
  "workPlansApplied": [
    {"planId":"PLAN-...","remoteId":"71","remoteVersion":3,"remoteUpdatedAt":"...","payloadHash":"sha256:..."}
  ],
  "remoteWorkPlans": [
    {"planId":"PLAN-...","remoteVersion":4,"remoteUpdatedAt":"...","payloadHash":"sha256:...","conflictToken":"opaque","document":{}}
  ],
  "workPlanConflicts": []
}
```

Use optimistic concurrency and conflict tokens exactly as for entity conflicts. Web approval/editing should update the WorkPlan document and remote version; local execution must still perform its own stale base-entity/workspace-definition checks before mutating domain data.
