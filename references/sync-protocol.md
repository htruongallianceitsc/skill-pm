# Sync Protocol v2

## Principles

- One HTTP endpoint handles entity data, project-definition, and governance/audit synchronization.
- Requests may be scoped by entity type and/or UID.
- The local folder remains usable offline.
- `uid` is the permanent cross-system correlation key.
- Server-assigned `id`/`code` are authoritative and become immutable locally after first mapping.
- Optimistic concurrency uses a remote version/ETag equivalent when available.
- Conflicts never overwrite either side silently.
- Force-local is explicit and should be narrowly scoped.

## Endpoint

```text
POST /api/project-manager/sync
```

## Request

```json
{
  "protocolVersion": "2.0",
  "project": {
    "uid": "project-uid",
    "id": null,
    "code": null
  },
  "client": {
    "instanceId": "client-instance-uid",
    "cursor": null
  },
  "scope": {
    "entityTypes": ["requirement", "test-case"],
    "entityUids": []
  },
  "options": {
    "direction": "bidirectional",
    "forceLocal": false,
    "includeDeleted": true
  },
  "workspaceDefinition": {
    "baseRemoteVersion": 3,
    "baseRemoteUpdatedAt": "2026-09-22T05:20:00Z",
    "payloadHash": "sha256:...",
    "conflictToken": null,
    "files": [
      {
        "path": ".pm/entity-types.json",
        "json": {}
      }
    ]
  },
  "changes": [
    {
      "operation": "upsert",
      "entityType": "requirement",
      "uid": "2adfca46-b57d-43d2-81f8-12b28c945532",
      "baseRemoteVersion": 4,
      "baseRemoteUpdatedAt": "2026-09-22T05:25:00Z",
      "payloadHash": "sha256:...",
      "conflictToken": null,
      "entity": {},
      "files": []
    }
  ],
  "audit": {
    "changeSets": [
      {
        "changeSetId": "CHG-...",
        "payloadHash": "sha256:...",
        "document": {}
      }
    ],
    "baselines": [
      {
        "baselineId": "BASE-...",
        "name": "release-1.2",
        "payloadHash": "sha256:...",
        "document": {}
      }
    ]
  }
}
```

`workspaceDefinition` may be null when unchanged or explicitly skipped. Empty scope arrays mean no restriction for that dimension.

## Response

```json
{
  "syncId": "sync-run-id",
  "serverTime": "2026-09-22T05:35:00Z",
  "nextCursor": "cursor-00231",
  "project": {
    "uid": "project-uid",
    "id": "87",
    "code": "PRJ-0087"
  },
  "definitionApplied": {
    "remoteVersion": 4,
    "remoteUpdatedAt": "2026-09-22T05:35:00Z",
    "payloadHash": "sha256:..."
  },
  "remoteDefinition": null,
  "applied": [
    {
      "entityType": "requirement",
      "uid": "2adfca46-b57d-43d2-81f8-12b28c945532",
      "id": "18422",
      "code": "REQ-00173",
      "remoteVersion": 5,
      "remoteUpdatedAt": "2026-09-22T05:35:00Z",
      "payloadHash": "sha256:..."
    }
  ],
  "remoteChanges": [],
  "auditApplied": [
    {"changeSetId": "CHG-...", "remoteId": "101", "remoteVersion": 1, "payloadHash": "sha256:..."}
  ],
  "baselinesApplied": [
    {"baselineId": "BASE-...", "remoteId": "31", "remoteVersion": 1, "payloadHash": "sha256:..."}
  ],
  "conflicts": [],
  "errors": []
}
```

## Workspace-definition pull/conflict

`remoteDefinition` uses the same `files` payload plus `remoteVersion`, `remoteUpdatedAt`, `payloadHash`, and optional `conflictToken`.

Apply it automatically only when the local workspace definition is clean relative to its last synced hash. Otherwise write snapshots under:

```text
.pm/conflicts/__workspace_definition__/<syncId>/
```

When the user explicitly chooses local, resend with `forceLocal: true` and the reviewed conflict token.

## Entity file payload

A file-mode content entry can travel in the same request:

```json
{
  "path": "files/test-script/<uid>/sign-in.spec.ts",
  "hash": "sha256:...",
  "contentType": "text/plain; charset=utf-8",
  "encoding": "utf-8",
  "content": "import { test } from '@playwright/test'; ..."
}
```

For large/binary files, evolve the server contract to blob references rather than inflating this request.

## Entity conflicts

A conflict should contain enough data for review:

```json
{
  "entityType": "requirement",
  "uid": "...",
  "reason": "remote_updated_after_base",
  "localUpdatedAt": "...",
  "remoteUpdatedAt": "...",
  "baseRemoteVersion": 4,
  "remoteVersion": 5,
  "conflictToken": "opaque-server-token",
  "remoteEntity": {},
  "remoteFiles": []
}
```

The client stores local and remote snapshots and leaves the working local entity unchanged.

## Force-local safety

A force-local request must carry the conflict token returned for the exact remote version reviewed. The server should reject stale tokens when remote data changed again.

## Sync state

Per entity:

```json
{
  "remoteVersion": 5,
  "remoteUpdatedAt": "...",
  "lastSyncedHash": "sha256:...",
  "lastSyncedAt": "...",
  "serverIdentity": {
    "id": "18422",
    "code": "REQ-00173"
  }
}
```

Workspace definition:

```json
{
  "remoteVersion": 4,
  "remoteUpdatedAt": "...",
  "lastSyncedHash": "sha256:...",
  "lastSyncedAt": "..."
}
```

Sync state never belongs inside domain entities.

## Audit and baseline sync

The same request carries changed ChangeSets and baselines in `audit`. The client compares each governance document's canonical hash with `.pm/sync-state.json`; unchanged documents are omitted. The server should deduplicate by `changeSetId`/`baselineId`, store the complete document, and acknowledge accepted hashes through `auditApplied` and `baselinesApplied`.

ChangeSets may change audit revision when a pending proposal is approved/rejected or an applied change is later marked with a revert event. Treat `(changeSetId, payloadHash)` idempotently and keep server history if desired. Baselines are immutable-style; replacing a named baseline locally is explicit and produces a new `baselineId`.

## WorkPlan sync

The same sync request carries changed WorkPlans under `planning.workPlans`. Scope may include `workPlanIds` when only selected plans should be exchanged.

Each item contains `planId`, base remote version/update timestamp, canonical `payloadHash`, optional reviewed `conflictToken` for force-local, and the complete WorkPlan document. The server should deduplicate by `planId + payloadHash` and return accepted items in `workPlansApplied`.

For web-originated changes, return `remoteWorkPlans`. The client auto-applies a remote WorkPlan only when its local hash still equals `workPlans[planId].lastSyncedHash`; otherwise it writes conflict snapshots under `.pm/conflicts/__workplans__/...` and keeps the live local plan unchanged. Explicit conflicts may also be returned in `workPlanConflicts`.

Per-plan sync bookkeeping belongs under `.pm/sync-state.json -> workPlans`, including `remoteId`, `remoteVersion`, `remoteUpdatedAt`, `lastSyncedHash`, `lastSyncedAt`, and any conflict token/path.
