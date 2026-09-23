# Project Doctor

## Purpose

`doctor` inspects local workspace integrity beyond basic schema validation and reports actionable findings without silently weakening project rules.

```bash
python scripts/pm_project.py doctor --project ./MyProject
```

The report can include:

- normal `validate` errors
- missing/deleted/mismatched relation targets
- stale, missing, or invalid derived manifests/indexes
- invalid saved-view query expressions
- deterministic semantic repair candidates

## Repair levels

### Safe

```bash
python scripts/pm_project.py doctor --project ./MyProject --fix
```

Safe repairs may:

- create expected local folders
- rebuild manifest/index caches
- replace invalid/stale generated index data

Safe repair must not edit business entity meaning, lifecycle status, content, relation intent, server identity, or governance records.

### Semantic

```bash
python scripts/pm_project.py doctor --project ./MyProject \
  --fix --fix-level semantic \
  --actor user:reviewer \
  --reason "Repair deterministic relation metadata"
```

Semantic repair is explicit. The starter implementation repairs a relation's declared `targetType` only when:

1. `targetUid` resolves to a real entity,
2. the real target entity type is known,
3. the source relation to that real target type is allowed by `.pm/relation-map.json`.

Such repairs increment entity revision/update time and create an audited ChangeSet. Do not auto-delete entities, invent missing relations, invent IDs/codes, change status, or fabricate missing content.

## Operational guidance

Run Doctor:

- after large manual filesystem edits
- after importing legacy project material
- before a release/baseline
- when search/query indexes appear stale
- when relationship validation fails unexpectedly

Treat Doctor as a repair assistant, not a replacement for `validate`, quality rules, or human review of business semantics.
