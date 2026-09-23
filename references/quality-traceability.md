# Quality and Traceability

## Goal

Keep project quality rules separate from structural validity. A requirement without a test case may be incomplete, but its JSON is still structurally valid. Model this as a quality finding rather than corrupt data.

## Rule file

Store rules in `.pm/quality-rules.json`.

Starter rule shape:

```json
{
  "id": "requirement-test-coverage",
  "label": "Approved requirements should have test coverage",
  "severity": "warning",
  "kind": "relation-count",
  "targetEntityTypes": ["requirement"],
  "statusIn": ["approved", "in_progress", "implemented"],
  "direction": "incoming",
  "relation": "verifies",
  "peerEntityTypes": ["test-case"],
  "min": 1
}
```

## Relation-count semantics

- `targetEntityTypes`: entities evaluated by the rule.
- `statusIn`: optional lifecycle states to which the rule applies.
- `direction`: `incoming` or `outgoing` relative to the target entity.
- `relation`: required relation type.
- `peerEntityTypes`: allowed source/target peer types; `*` means any.
- `min`: minimum matching relation count.
- `severity`: `info`, `warning`, or `error`.

## Starter checks

The template includes configurable rules for:

- approved/in-progress/implemented requirements having test-case coverage;
- planned/in-progress/done features having requirement traceability;
- ready/passed/failed test cases having an automation script (informational by default);
- active bugs identifying an affected entity;
- active tasks linking to project scope.

These are defaults, not universal truths. Edit or delete them per project.

## Generated report

`quality` writes `.pm/reports/quality-report.json`:

```json
{
  "generatedAt": "...",
  "summary": { "info": 2, "warning": 1 },
  "findings": [
    {
      "ruleId": "requirement-test-coverage",
      "severity": "warning",
      "uid": "...",
      "entityType": "requirement",
      "title": "User can sign in",
      "observed": 0,
      "minimum": 1
    }
  ]
}
```

Reports are generated caches and should not be treated as canonical project data.

## Sync gating

`.pm/config.json` may set:

```json
{
  "validation": {
    "qualityRulesBlockSyncAtSeverity": "error"
  }
}
```

With `error`, informational and warning findings remain visible but do not block synchronization. Use stricter policy only when the team explicitly wants it.

## Rule categories and metrics

Add `category` to rules when clients need grouped views such as `test-coverage`, `automation-coverage`, or `traceability`. The CLI quality report includes per-rule `applicable`, `passed`, `failed`, and `coveragePercent` metrics. `missing-tests` is simply a filtered view of rules categorized `test-coverage`; it does not hard-code requirement or test entity types.
