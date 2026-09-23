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
