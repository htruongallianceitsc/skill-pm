# Starter Entity Catalog

The template starts with a useful software-project catalog. It is a default, not a hard-coded limit.

| Entity type | Typical purpose | Notable starter data |
|---|---|---|
| `document` | Markdown/HTML project documents and notes | document type, owner, summary, audience |
| `module` | Functional/system modules | owner, scope, order |
| `feature` | User/system features | priority, feature type, owner, target release |
| `requirement` | Functional/non-functional requirements | priority, requirement type, acceptance criteria, source, rationale |
| `business-rule` | Business constraints/rules | rule type, priority, expression/effective dates |
| `screen` | UI screens/views/routes | route, platform, component/design metadata |
| `api` | API endpoints/contracts | method, path, auth/content-type/version metadata |
| `database-table` | Tables/views/materialized structures | schema/name/object type/primary key/engine |
| `task` | Work assignments | priority, task type, assignee, due date, estimates |
| `bug` | Defects | severity, priority, environment, reproduction/expected/actual result |
| `test-case` | Manual/automatable test cases | priority, type, preconditions, structured steps, expected result |
| `test-script` | Playwright/other executable tests | framework, language, command, enabled flag |

Each type also has a configurable lifecycle and UI definition in `.pm/entity-types.json` and a type-specific `dataSchema`.

## Adding a type

Add a registry entry with at least:

```json
{
  "release": {
    "displayName": "Release",
    "folder": "entities/releases",
    "schema": "schemas/core/entity.schema.json",
    "dataSchema": "schemas/entities/release.data.schema.json",
    "codePrefix": "REL",
    "category": "delivery",
    "defaults": {
      "status": "draft",
      "data": {},
      "content": []
    },
    "lifecycle": {
      "initialStatus": "draft",
      "statuses": {
        "draft": { "label": "Draft", "terminal": false }
      },
      "transitions": {}
    },
    "ui": {
      "list": { "columns": ["code", "title", "status"] },
      "filters": ["status"],
      "searchFields": ["code", "title", "tags"],
      "form": { "sections": [] }
    }
  }
}
```

Then create the folder/schema and add relation/quality rules only when needed.


## v9 Module semantics

Treat `module` as a functional **feature group**, not as a generic container for arbitrary entities. Example: module `AUTH` contains features `Login`, `Register`, and `Forgot Password`. Store the durable edge on each Feature as `feature --belongs_to--> module`. Requirements, screens, APIs, rules, and tests should normally trace through Features rather than being directly owned by a Module.

## Decision

Use `decision` for durable business/product/technical decisions and resolved project questions. Starter lifecycle is `proposed -> accepted/rejected`, with accepted decisions later becoming `superseded` rather than overwritten. Store context, decision, rationale, consequences, source request id, and decision actor/time. Link Decisions to affected durable entities using configured relation mappings.
