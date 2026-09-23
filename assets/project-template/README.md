# Local Project Workspace

This folder is a portable project database. It can be managed locally without the Project Manager API and synchronized later.

## Core files

- `project.json`: project identity/settings.
- `.pm/entity-types.json`: entity registry, lifecycle, and generic UI configuration.
- `.pm/relation-map.json`: dynamic relationship rules.
- `.pm/lookups.json`: reusable select/enum catalogs.
- `.pm/quality-rules.json`: traceability and coverage policy.
- `.pm/intelligence.json`: configurable graph traversal, AI context, impact, orphan, and health behavior.
- `.pm/change-management.json`: audit/review/undo/baseline policy.
- `.pm/agent-workflow.json`: AI WorkPlan planning/review/execution policy.
- `.pm/local-engine.json`: local indexing/query limits and Doctor policy.
- `.pm/views.json`: saved local views (query + columns/sort/grouping only).
- `.pm/workplans/**`: portable WorkPlan records; completed plans point to their ChangeSet.
- `.pm/changesets/**`, `.pm/baselines/**`: canonical governance history.
- `.pm/audit-state.json`: local snapshot used to detect direct/manual edits.
- `schemas/entities/*.data.schema.json`: type-specific validation contracts.
- `entities/**`: canonical entity JSON records.
- `files/**`: optional long-form/executable content.
- `.pm/sync-state.json`: local remote-sync bookkeeping.
- `.pm/indexes/**`, `.pm/manifest.json`, `.pm/reports/**`: generated data that can be rebuilt.

## Identity rule

Use `uid` for every local relationship. Leave `id` and `code` null until the server assigns them. After server assignment, treat `id` and `code` as immutable.

## Generic clients

A client should be able to generate navigation, CRUD forms, list columns, filters, lifecycle actions, and relation pickers from the JSON configuration without hard-coding the starter entities.

## Single-file mode

Use `bundle-export` to package project metadata, definitions, entities, referenced UTF-8 content, WorkPlans, ChangeSets, baselines, and audit state into one `project.bundle.json`. A browser or desktop client can manage that JSON independently, then `bundle-import` it back into folder form.

## Local search, query, and Doctor

```bash
python scripts/pm_project.py reindex --project ./MyProject
python scripts/pm_project.py search --project ./MyProject --text "reset password"
python scripts/pm_project.py query --project ./MyProject --expr 'type=bug AND status!=closed AND data.severity=critical'
python scripts/pm_project.py query --project ./MyProject --expr 'type=test-case' --related-to FEAT-001 --max-depth 2
python scripts/pm_project.py view-list --project ./MyProject
python scripts/pm_project.py view-run --project ./MyProject --view open-bugs
python scripts/pm_project.py doctor --project ./MyProject
python scripts/pm_project.py doctor --project ./MyProject --fix
```

Indexes are disposable caches. `doctor --fix` performs safe cache/folder repairs only; use `--fix-level semantic` explicitly for deterministic entity metadata repair.

## Project intelligence

Useful local commands:

```bash
python scripts/pm_project.py context --project ./MyProject --ref REQ-001
python scripts/pm_project.py impact --project ./MyProject --ref API-014
python scripts/pm_project.py coverage --project ./MyProject
python scripts/pm_project.py missing-tests --project ./MyProject
python scripts/pm_project.py broken-links --project ./MyProject
python scripts/pm_project.py orphans --project ./MyProject
python scripts/pm_project.py health --project ./MyProject
```

These commands derive behavior from project configuration rather than a fixed entity hierarchy.

## Change management

Normal CLI mutations automatically create audited ChangeSets. For direct editor changes, run `audit-scan`. For AI changes requiring approval, stage them with `propose` and apply only through `review-approve`.

```bash
python scripts/pm_project.py history --project ./MyProject --ref REQ-001
python scripts/pm_project.py diff --project ./MyProject --ref REQ-001
python scripts/pm_project.py propose --project ./MyProject --ref REQ-001 --patch-json '{"data":{"priority":"high"}}' --actor ai:agent --reason "Impact review"
python scripts/pm_project.py reviews --project ./MyProject
python scripts/pm_project.py baseline-create --project ./MyProject --name release-1.0 --release 1.0.0
python scripts/pm_project.py baseline-compare --project ./MyProject --from release-1.0 --to current
```

## Agent WorkPlans

For substantial AI-driven changes, plan before mutating project entities:

```bash
python scripts/pm_project.py plan-context --project ./MyProject --request "Add reset password" --ref MOD-AUTH --include-impact
python scripts/pm_project.py plan-create --project ./MyProject --request "Add reset password" --spec-file ./reset-plan.json --reason "Requested capability"
python scripts/pm_project.py plan-validate --project ./MyProject --plan PLAN-...
python scripts/pm_project.py plan-submit --project ./MyProject --plan PLAN-...
python scripts/pm_project.py plan-show --project ./MyProject --plan PLAN-...
python scripts/pm_project.py plan-approve --project ./MyProject --plan PLAN-... --actor user:reviewer
python scripts/pm_project.py plan-execute --project ./MyProject --plan PLAN-... --actor ai:agent
```

Use `$step:S1` or `$handle:feature-name` to reference entities created earlier in the same plan. Submission captures base hashes; approval/execution stop when reviewed entities or workspace definitions have changed. Successful execution produces one linked ChangeSet.

## Source code and Git intelligence

The source index is disposable and does not turn every code file into an entity:

```bash
python scripts/pm_project.py scan-source --project ./MyProject
python scripts/pm_project.py source-map --project ./MyProject --path src/auth/login.ts
python scripts/pm_project.py git-status --project ./MyProject
python scripts/pm_project.py git-impact --project ./MyProject --commit HEAD
python scripts/pm_project.py commit-context --project ./MyProject --commit HEAD
```

Configure scan roots and mapping rules in `.pm/source-intelligence.json`. Technical importers are discovery-only unless `--apply` is supplied:

```bash
python scripts/pm_project.py scan-openapi --project ./MyProject --path api/openapi.yaml
python scripts/pm_project.py scan-database --project ./MyProject --path db/schema.sql --engine PostgreSQL
python scripts/pm_project.py scan-playwright --project ./MyProject --path tests
python scripts/pm_project.py import-markdown --project ./MyProject --path docs/architecture.md
```

When an implementation ChangeSet already exists, attach the final commit with `git-link`. Use `git-changeset` only for historical/external commit evidence; evidence ChangeSets are not reversible.

## Code dependency, regression tests, and change planning

Build and inspect the derived local code graph:

```bash
python scripts/pm_project.py code-deps --project ./MyProject --path src/auth/login.ts --direction both
python scripts/pm_project.py dependency-tree --project ./MyProject --path src/auth/login.ts --direction dependents
python scripts/pm_project.py circular-dependencies --project ./MyProject
```

Analyze current changes through both code and project graphs, then select likely regression tests:

```bash
python scripts/pm_project.py code-impact --project ./MyProject --working-tree
python scripts/pm_project.py changed-tests --project ./MyProject --commit HEAD
python scripts/pm_project.py test-selection --project ./MyProject --path src/auth/auth.service.ts
```

Create a reviewable planning shell from code changes:

```bash
python scripts/pm_project.py plan-from-git --project ./MyProject --commit HEAD --actor ai:agent --reason "Review commit impact"
python scripts/pm_project.py plan-from-diff --project ./MyProject --working-tree --actor ai:agent --reason "Review working tree"
```

Generated plans intentionally contain no executable mutation steps. Review the impact/tests, then use `plan-amend` to author deterministic steps before `plan-submit`. The dependency index is disposable and can be rebuilt with `reindex`.


## Documentation-first v9

- Start an empty project with `idea-bootstrap`.
- Trace project questions/requests under `.pm/requests/`.
- `docs/project-graph.html` is generated from entities and relations.
- Generate readable standalone HTML diagrams with `diagram-create`.
- Implementation is intentionally gated behind the explicit `implement` command.

## V10 Project Consistency & Governance

Use `doc-check`, `traceability`, `dashboard-build`, `request-promote`, `decision-create`, `ready-check`, and `done-check` to keep documentation, scope, tasks, decisions, and implementation consistent.
