# Git and Source Introspection

## Purpose

Keep source code as local evidence rather than duplicating every source file as a project entity. The durable model remains the JSON entity graph; `.pm/indexes/source-index.json` is disposable and rebuildable.

## Configuration

Use `.pm/source-intelligence.json`:

- `scan.roots`: source/test/spec/database folders to scan.
- `scan.includeExtensions` / `includeNames`: supported text source files.
- `scan.ignoreGlobs`: generated/vendor/project-management folders to exclude.
- `scan.maxFileBytes`: upper bound for lightweight local analysis.
- `mapping.scanEntityCodes`: map literal server codes mentioned in source.
- `mapping.scanLocalRefs`: optional localRef mention matching.
- `mapping.scanKeyFields`: technical identifiers whose literal appearance is useful evidence, such as API path, operationId, database table name, screen route, or test-script path.
- `mapping.explicit`: user-maintained path-glob to entity-ref mappings for ambiguous codebases.

Example explicit mapping:

```json
{
  "path": "src/auth/**",
  "entityRef": "FEAT-AUTH"
}
```

## Source index

Run:

```bash
python scripts/pm_project.py scan-source --project ./Project
```

The generated index records per-file:

- project-relative path
- hash, bytes, line count
- detected language
- lightweight symbols
- imports/usings
- `entityRefs[]` with mapping kind and confidence

Mapping evidence is intentionally inspectable. Prefer:

1. `entity-json`, `content-reference`, `data-path`, `explicit` = exact
2. `code-mention`, `technical-identifier` = medium

Do not silently promote medium-confidence evidence into permanent entity relations.

Use:

```bash
python scripts/pm_project.py source-map --project ./Project --path src/auth/login.ts
python scripts/pm_project.py source-map --project ./Project --ref API-014
```

## Git workflow

### Working tree

```bash
python scripts/pm_project.py git-status --project ./Project
```

Returns Git status plus project entities mapped from changed paths.

### Commit/range changes

```bash
python scripts/pm_project.py changes-since-commit \
  --project ./Project \
  --from-ref v1.2.0 \
  --to-ref HEAD
```

### Potential impact

```bash
python scripts/pm_project.py git-impact \
  --project ./Project \
  --commit HEAD \
  --max-depth 3
```

The algorithm is:

1. collect changed source paths;
2. map changed paths to direct entities using source evidence;
3. run the configured project `impact` graph profile from each direct entity;
4. merge/dedupe impacted entities and retain `impactDepth` / `impactRoots`.

Always describe this as graph-based **potential impact**, not proven runtime impact.

### Commit context

```bash
python scripts/pm_project.py commit-context \
  --project ./Project \
  --commit HEAD \
  --detail summary
```

Use this before code review, regression-test selection, or creating a WorkPlan from an existing commit.

## Git ↔ ChangeSet linkage

When entity edits were already audited as a ChangeSet, link the final commit:

```bash
python scripts/pm_project.py git-link \
  --project ./Project \
  --change-set CHG-... \
  --commit HEAD
```

If a historical/external commit has no captured entity before/after state, create an evidence record:

```bash
python scripts/pm_project.py git-changeset \
  --project ./Project \
  --commit HEAD \
  --actor user:reviewer \
  --reason "Imported historical implementation evidence"
```

An evidence ChangeSet is intentionally non-reversible. It preserves commit metadata, changed files, and mapped project entities; `undo` semantics do not apply.

## Technical artifact importers

Discovery is non-mutating by default. Add `--apply` only after reviewing candidates.

### OpenAPI

```bash
python scripts/pm_project.py scan-openapi --project ./Project --path api/openapi.json
python scripts/pm_project.py scan-openapi --project ./Project --path api/openapi.yaml --apply
```

Creates one `api` entity per unique `(method, path)` and references the source spec file. JSON is parsed structurally; YAML uses a conservative paths/method/operationId/summary reader rather than a general YAML implementation.

### SQL DDL

```bash
python scripts/pm_project.py scan-database \
  --project ./Project \
  --path db/schema.sql \
  --engine PostgreSQL \
  --apply
```

Discovers `CREATE TABLE`, `CREATE VIEW`, and `CREATE MATERIALIZED VIEW`, including simple column/primary-key evidence. SQL parsing is intentionally lightweight; treat generated metadata as a starting point for review.

### Playwright

```bash
python scripts/pm_project.py scan-playwright \
  --project ./Project \
  --path tests \
  --apply
```

Creates one `test-script` entity per discovered Playwright spec file, references the real script, and records test names/line evidence under `data`.

### Markdown

```bash
python scripts/pm_project.py import-markdown \
  --project ./Project \
  --path docs/authentication.md
```

Creates a `document` entity, derives a title/summary, and keeps a project-relative Markdown content reference. External files are copied into the entity's portable `files/` folder.

## Recommended coding-agent sequence

For an existing repository:

1. `init` the project workspace in/at the repository root.
2. Configure `.pm/source-intelligence.json` roots/ignore rules.
3. Run technical importers in discovery mode.
4. Review, then `--apply` desired API/database/test/document entities.
5. Link imported entities to Features/Requirements using explicit project knowledge; do not infer permanent relations solely from source heuristics.
6. `scan-source`.
7. Use `git-impact` / `commit-context` before code review or regression planning.
8. Use WorkPlans for substantial project-model changes.
9. Link final Git commit to the resulting ChangeSet.
