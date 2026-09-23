# Schema-Driven Runtime

## Purpose

A project must remain editable by an AI agent, CLI, desktop app, or generic web UI without hard-coded knowledge of `requirement`, `bug`, `task`, or any other starter type. The runtime contract is data-driven.

## Configuration layers

### `.pm/entity-types.json`

Registry for every entity type. Each entry defines:

- storage folder;
- display/category metadata;
- official code prefix hint;
- common defaults;
- `dataSchema` path;
- lifecycle statuses and transitions;
- list columns, filters, search fields, and form sections.

Example:

```json
{
  "requirement": {
    "displayName": "Requirement",
    "folder": "entities/requirements",
    "dataSchema": "schemas/entities/requirement.data.schema.json",
    "codePrefix": "REQ",
    "lifecycle": {
      "initialStatus": "draft",
      "statuses": {
        "draft": { "label": "Draft", "terminal": false },
        "approved": { "label": "Approved", "terminal": false }
      },
      "transitions": {
        "draft": ["approved"]
      }
    },
    "ui": {
      "list": { "columns": ["code", "title", "status", "data.priority"] },
      "filters": ["status", "data.priority"],
      "searchFields": ["code", "title", "tags"],
      "form": {
        "sections": [
          { "key": "general", "fields": ["title", "status", "tags"] },
          { "key": "details", "fields": ["data.priority", "data.acceptanceCriteria"] }
        ]
      }
    }
  }
}
```

### `schemas/entities/*.data.schema.json`

Standard JSON Schema documents validating only the entity's `data` object. This separation keeps the shared envelope stable while allowing each type to evolve independently.

Custom annotations such as `x-lookup` are safe because JSON Schema validators ignore unknown keywords.

Example:

```json
{
  "type": "object",
  "properties": {
    "priority": {
      "type": "string",
      "enum": ["low", "normal", "high", "critical"],
      "x-lookup": "priority"
    }
  },
  "additionalProperties": true
}
```

### `.pm/lookups.json`

Reusable option catalogs. A web form can resolve `x-lookup: priority` into labels/order without coding the values into components.

### `.pm/relation-map.json`

Defines which entity types may link, relation names, cardinality, and whether unresolved targets are allowed.

### `.pm/quality-rules.json`

Defines non-structural project quality expectations. These should produce findings, not mutate entities.

## Generic CRUD UI algorithm

1. Load the entity registry.
2. Build navigation from configured types/categories.
3. For a list page, read `ui.list.columns`, `ui.filters`, and `ui.searchFields`.
4. Resolve dotted fields such as `data.priority` from each entity.
5. For a create/edit form, load the type's `dataSchema` and `ui.form.sections`.
6. Render core fields (`title`, `status`, `tags`) using the common envelope.
7. Render `data.*` controls from JSON Schema type/enum and optional `x-lookup`.
8. Render status choices from lifecycle configuration, limiting next status choices to configured transitions.
9. Render relation pickers from relation-map mappings valid for the source entity type.
10. Save the entity JSON, increment revision, update timestamp, validate, then rebuild indexes.

## Suggested field rendering rules

- `type: string` -> text input.
- `type: number/integer` -> numeric input.
- `type: boolean` -> checkbox/switch.
- `enum` or `x-lookup` -> select/autocomplete.
- `format: date-time` -> date-time picker.
- `type: array` of string -> tags/repeatable text rows.
- `type: array` of objects -> editable grid/repeater.
- `type: object` -> nested form or JSON editor fallback.

Always provide a JSON editor fallback for unknown/custom schemas so new types remain usable before custom UI exists.

## Adding an entity type without core-code changes

1. Add the registry entry.
2. Create its entity folder.
3. Add a data schema.
4. Add lookup values if needed.
5. Add lifecycle/UI configuration.
6. Add relation mappings if it participates in links.
7. Add quality rules only when desired.
8. Run `describe`, `validate`, and `manifest`.

The CLI and generic client should not need a new switch/case for the type.
