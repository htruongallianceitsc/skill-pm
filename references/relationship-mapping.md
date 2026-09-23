# Relationship Mapping

## Purpose

Relationships are data-driven. The engine must not hard-code that a feature belongs to a module, a test case verifies a requirement, or a task can relate to everything. Those are rules in `.pm/relation-map.json` and can be changed per project.

## Format

```json
{
  "schemaVersion": "1.0",
  "relationTypes": {
    "belongs_to": {
      "label": "Belongs to",
      "reverse": "contains"
    },
    "verifies": {
      "label": "Verifies",
      "reverse": "verified_by"
    }
  },
  "mappings": [
    {
      "key": "feature.belongs_to.module",
      "from": ["feature"],
      "relation": "belongs_to",
      "to": ["module"],
      "cardinality": "many-to-one",
      "required": false,
      "allowUnresolved": false
    }
  ]
}
```

## Matching rules

A relation is valid when at least one mapping matches:

- source `entityType` is present in `from`, or `from` contains `*`;
- relation `type` equals `relation`;
- target `entityType` is present in `to`, or `to` contains `*`.

## Cardinality

Supported starter values:

- `one-to-one`
- `one-to-many`
- `many-to-one`
- `many-to-many`

Cardinality is evaluated from the source side of the stored relation. For example, `feature belongs_to module` with `many-to-one` means each feature may point to at most one module through that relation, while many features can target the same module.

## Wildcard example

Tasks and bugs may use a generic relation without making the core aware of every possible target:

```json
{
  "key": "work-item.relates-to.any",
  "from": ["task", "bug"],
  "relation": "relates_to",
  "to": ["*"],
  "cardinality": "many-to-many",
  "required": false,
  "allowUnresolved": false
}
```

## Reverse relations

Do not duplicate reverse relations into the target entity by default. Build reverse lookup from the generated relation index. This avoids two files needing to be updated atomically for every link change.

If a project explicitly needs materialized two-way links, model that as an optional policy rather than a core assumption.

## Changing the graph

To add a new relationship:

1. Add the relation type if it is new.
2. Add a mapping row.
3. Run validation.
4. Rebuild the relation index.

Existing entity JSON does not need migration unless an old relation becomes invalid under the new mapping.
