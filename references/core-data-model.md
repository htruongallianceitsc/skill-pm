# Core Data Model

## Common entity envelope

```json
{
  "schemaVersion": "1.0",
  "entityType": "requirement",
  "uid": "2adfca46-b57d-43d2-81f8-12b28c945532",
  "id": null,
  "code": null,
  "localRef": "local:requirement:2adfca46",
  "title": "User can sign in",
  "status": "draft",
  "isDeleted": false,
  "deletedAt": null,
  "tags": ["authentication"],
  "relations": [],
  "data": {},
  "content": [],
  "revision": 1,
  "createdAt": "2026-09-22T05:30:00Z",
  "updatedAt": "2026-09-22T05:30:00Z"
}
```

## Identity lifecycle

### Local-only

```json
{
  "uid": "2adfca46-b57d-43d2-81f8-12b28c945532",
  "id": null,
  "code": null,
  "localRef": "local:requirement:2adfca46"
}
```

The UI should display `code` when present, otherwise `localRef`.

### After first successful sync

```json
{
  "uid": "2adfca46-b57d-43d2-81f8-12b28c945532",
  "id": "18422",
  "code": "REQ-00173",
  "localRef": "local:requirement:2adfca46"
}
```

The corresponding sync-state entry stores the server identity snapshot:

```json
{
  "serverIdentity": {
    "id": "18422",
    "code": "REQ-00173"
  }
}
```

From this point, a local edit to `id` or `code` is invalid.

## Type-specific data

Keep fields unique to a type under `data`. Examples:

Requirement:

```json
{
  "data": {
    "priority": "high",
    "acceptanceCriteria": [
      "A valid user can sign in",
      "Invalid credentials show an error"
    ]
  }
}
```

Task:

```json
{
  "data": {
    "priority": "normal",
    "assignee": null,
    "dueDate": null,
    "estimateHours": null
  }
}
```

Bug:

```json
{
  "data": {
    "severity": "high",
    "priority": "high",
    "environment": "UAT",
    "stepsToReproduce": []
  }
}
```

## Content model

`content` is an array so one entity can carry a body, an attachment-like source file, and generated output without adding custom fields.

Inline Markdown:

```json
{
  "content": [
    {
      "name": "body",
      "format": "markdown",
      "mode": "inline",
      "body": "# Authentication\n..."
    }
  ]
}
```

Referenced Playwright script:

```json
{
  "content": [
    {
      "name": "script",
      "format": "typescript",
      "mode": "file",
      "path": "files/test-script/2adfca46-b57d-43d2-81f8-12b28c945532/sign-in.spec.ts"
    }
  ]
}
```

All file paths are project-relative. Reject absolute paths and any path containing `..` traversal.

## Relations

```json
{
  "relations": [
    {
      "type": "belongs_to",
      "targetUid": "98613385-0a4b-4f13-9518-a225abf72d93",
      "targetType": "feature",
      "metadata": {}
    }
  ]
}
```

`targetUid` is authoritative. `targetType` is retained so a human and validator can detect mistakes quickly.

## Revision fields

- `revision`: local semantic revision. Increment on local domain changes.
- `updatedAt`: local semantic update time.
- Remote versions and remote timestamps belong in `.pm/sync-state.json`, not in the entity.

This prevents a sync bookkeeping update from making a domain entity appear locally modified.
