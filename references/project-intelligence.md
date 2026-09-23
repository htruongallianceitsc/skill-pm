# Project Intelligence

## Purpose

Use the project graph and configurable quality rules to retrieve focused context, analyze potential change impact, inspect coverage, and detect structural gaps without hard-coding a particular project hierarchy.

The intelligence engine reads:

- `.pm/entity-types.json` for entity definitions.
- `.pm/relation-map.json` for valid graph edges.
- `.pm/quality-rules.json` for coverage and traceability expectations.
- `.pm/intelligence.json` for traversal profiles and reporting behavior.
- canonical entity JSON files for live data.

Generated indexes can accelerate clients, but the CLI intelligence commands operate from canonical entity files so stale indexes cannot silently change an answer.

## Entity selectors

Commands that accept `--ref` resolve in this order:

1. exact `uid`;
2. exact server `id`;
3. exact server `code`;
4. exact `localRef`;
5. unique exact title when enabled by `.pm/intelligence.json`.

If a title matches multiple entities, fail instead of guessing.

## Traversal profiles

Configure graph traversal under `.pm/intelligence.json`:

```json
{
  "profiles": {
    "context": {
      "maxDepth": 2,
      "direction": "both",
      "relations": ["*"],
      "entityTypes": ["*"],
      "maxEntities": 40
    },
    "impact": {
      "maxDepth": 3,
      "direction": "both",
      "relations": ["depends_on", "implements", "verifies"],
      "entityTypes": ["*"],
      "maxEntities": 100
    }
  }
}
```

These are starter profiles, not domain laws. Change them for each project. The engine never assumes Requirement -> Feature -> Test Case unless the configured graph actually contains those relationships.

## Trace

Use `trace` when an agent or user needs the relationship neighborhood of one entity.

```bash
python scripts/pm_project.py trace \
  --project ./MyProject \
  --ref REQ-001 \
  --profile context \
  --max-depth 2
```

The response includes shortest discovered paths, nodes grouped by depth, and the real stored relation edges.

## AI context pack

Use `context` before implementing, reviewing, or discussing one project object.

```bash
python scripts/pm_project.py context \
  --project ./MyProject \
  --ref SCREEN-012 \
  --detail summary
```

`summary` includes identity, status, tags, type-specific `data`, relation graph, content references, and relevant quality findings. `full` also includes the full entity envelope and bounded UTF-8 file content.

Prefer `summary` first. Load full files only when the task requires their exact content. This reduces agent context consumption and avoids reading the whole project.

## Impact analysis

Use `impact` before changing an entity with known downstream or cross-cutting relationships.

```bash
python scripts/pm_project.py impact \
  --project ./MyProject \
  --ref API-014
```

The result is a set of **potentially related/affected** entities based on the configured `impact` traversal profile. Treat it as graph evidence, not proof of runtime impact. Refine `.pm/intelligence.json` when a project needs narrower semantics.

## Coverage

Quality rules can declare categories such as `test-coverage`, `automation-coverage`, or `traceability`.

```bash
python scripts/pm_project.py coverage --project ./MyProject
python scripts/pm_project.py coverage --project ./MyProject --ref REQ-001
python scripts/pm_project.py coverage --project ./MyProject --category traceability
```

Each configured relation-count rule reports:

- applicable entity count;
- passed count;
- failed count;
- coverage percentage when at least one entity is applicable;
- detailed findings for failures.

Use `missing-tests` as the convenience view for rules categorized `test-coverage`.

## Orphans and broken links

`orphans` lists non-deleted entities that have no valid incoming or outgoing relation after configured exclusions.

`broken-links` detects:

- missing targets;
- relations to soft-deleted targets;
- declared target type mismatch;
- relation edges no longer allowed by `.pm/relation-map.json`.

These are diagnostic views. An orphan is not automatically invalid because some project object types are legitimately standalone.

## Project health

Use `health` for a factual project snapshot:

```bash
python scripts/pm_project.py health --project ./MyProject
```

It reports counts rather than inventing an opaque score:

- structural/schema validation errors;
- quality findings and per-rule coverage;
- broken link and orphan counts;
- active entities by type/status;
- local-only vs server-assigned identities;
- dirty entities, last sync, cursor, and unresolved conflict snapshot count.

Consumers may define their own dashboard score from these metrics, but the portable core should preserve the raw evidence.

## Agent workflow

For a change request referencing a known entity:

1. Resolve the entity with `context --detail summary`.
2. Run `impact` when the change can affect related scope.
3. Load exact Markdown/HTML/API/SQL/test content only for entities needed by the task.
4. Make local edits while preserving immutable identity rules.
5. Run `validate`.
6. Run `quality` or the relevant `coverage` view.
7. Run `health` for larger batches or pre-release checks.
8. Sync only after local validation passes and conflicts have been reviewed.
