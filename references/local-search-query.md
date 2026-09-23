# Local Search, Query, and Saved Views

## Purpose

Keep the project folder usable as a fast local knowledge base without introducing a database as the source of truth. All indexes are disposable derived files under `.pm/indexes/` and can be deleted/rebuilt from entity JSON plus referenced content.

## Local full-text index

`reindex` rebuilds:

- `.pm/manifest.json`
- `.pm/indexes/entity-index.json`
- `.pm/indexes/relation-index.json`
- `.pm/indexes/search-index.json`

The search index includes configurable text from:

- identity/metadata (`uid`, `id`, `code`, `localRef`, title, status, tags)
- flattened `data.*` values
- inline `content[]`
- referenced UTF-8 files such as Markdown, HTML, SQL, OpenAPI text, TypeScript, and Playwright specs

Configure limits in `.pm/local-engine.json`. Never edit the generated search index as project data.

Examples:

```bash
python scripts/pm_project.py reindex --project ./MyProject
python scripts/pm_project.py search --project ./MyProject --text "reset password"
python scripts/pm_project.py search --project ./MyProject --text withdrawal --type api --type requirement
```

`search` automatically rebuilds a missing/stale index unless `--no-reindex` is used.

## Query expression language

Use `query --expr` for deterministic field filtering.

Supported boolean syntax:

```text
type=bug AND status!=closed
type=requirement AND (status=approved OR status=in_progress)
type=api AND data.method=POST AND data.path~=/withdrawal
NOT status=deprecated
```

Supported comparison operators:

- `=` equal; array fields treat equality as membership
- `!=` not equal/not a member
- `~=` case-insensitive contains
- `^=` starts with
- `$=` ends with
- `>`, `>=`, `<`, `<=` numeric when both sides are numeric, otherwise lexical

Field rules:

- `type` aliases `entityType`
- `tag` aliases `tags`
- normal envelope fields can be addressed directly (`status`, `title`, `code`, `updatedAt`)
- type-specific values use `data.<field>`
- quoted strings may contain spaces and Unicode text

Examples:

```bash
python scripts/pm_project.py query --project ./MyProject \
  --expr 'type=bug AND status!=closed AND data.severity=critical'

python scripts/pm_project.py query --project ./MyProject \
  --expr 'type=api AND data.method=POST AND data.path~="withdrawal"'
```

## Graph-scoped query

Limit a query to entities reachable around another entity:

```bash
python scripts/pm_project.py query --project ./MyProject \
  --expr 'type=test-case' \
  --related-to FEAT-012 \
  --direction both \
  --max-depth 2
```

Optionally repeat `--relation` to restrict traversal. Graph scope uses `uid` relations and current project data; it does not infer undocumented dependencies.

## Saved views

`.pm/views.json` stores reusable query/presentation definitions. A view may define:

- `expression`
- graph scope (`relatedTo`, `direction`, `relations`, `maxDepth`)
- `columns`
- `sort`
- optional explicit sort value order (for example severity `critical, high, medium, low`)
- `groupBy`
- `limit`
- `includeDeleted`

Commands:

```bash
python scripts/pm_project.py view-list --project ./MyProject
python scripts/pm_project.py view-show --project ./MyProject --view open-bugs
python scripts/pm_project.py view-run --project ./MyProject --view open-bugs
```

Saved views never duplicate project entity data. A generic local/web client can use the same view definitions to render tables/boards later.
