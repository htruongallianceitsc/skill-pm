# Code Dependency and Change Intelligence

## Purpose

Use a disposable source dependency graph to reason from changed files to dependent code, mapped project entities, and regression tests. Keep this graph separate from durable project relations: imports are implementation evidence, not business truth.

## Dependency index

`reindex` or `code-deps --rebuild` generates `.pm/indexes/dependency-index.json` from `.pm/indexes/source-index.json`. It records resolved local imports as outgoing/incoming edges. Delete it at any time and rebuild.

Configure `.pm/source-intelligence.json` under `dependency`:

- `aliases[]`: `{ "prefix": "@/", "target": "src/" }` style local import aliases.
- `testGlobs[]`: patterns used for regression-test discovery.
- `maxDepth`: project convention/default depth for future clients.

Resolution is conservative. Relative JS/TS/Dart and Python imports are strongest; language/module imports may be high-confidence heuristics. Unresolved external packages are retained as metadata but do not become graph edges.

## Explore dependencies

```bash
python scripts/pm_project.py code-deps --project ./Project --path src/auth/login.ts --direction both --max-depth 3
python scripts/pm_project.py dependency-tree --project ./Project --path src/auth/login.ts --direction dependents
python scripts/pm_project.py circular-dependencies --project ./Project
```

For change analysis, prefer `dependents`: if `auth.service.ts` changed, files importing it are potential code impact.

## Code impact

```bash
python scripts/pm_project.py code-impact --project ./Project --working-tree
python scripts/pm_project.py code-impact --project ./Project --commit HEAD
python scripts/pm_project.py code-impact --project ./Project --from-ref main --to-ref HEAD
python scripts/pm_project.py code-impact --project ./Project --path src/auth/login.ts
```

Pipeline:

1. determine changed source paths;
2. traverse source **dependents**;
3. map impacted files to project entities using inspectable source evidence;
4. expand those entities through the configured durable project impact graph.

Always call the result **potential impact**. Static imports cannot prove runtime behavior.

## Regression test selection

```bash
python scripts/pm_project.py test-selection --project ./Project --working-tree
python scripts/pm_project.py changed-tests --project ./Project --commit HEAD
```

Tests are selected from two evidence channels:

- source test files that are dependents of changed/impacted code;
- `test-script` entities reached through the project impact graph.

Return commands only when there is enough evidence (for example a Test Script's configured command, Playwright import, pytest file, or Flutter test path). Do not invent a test runner.

## Planning from code changes

```bash
python scripts/pm_project.py plan-from-git --project ./Project --commit HEAD --actor ai:agent
python scripts/pm_project.py plan-from-diff --project ./Project --working-tree --actor ai:agent
```

These commands create a **draft planning shell**, not executable mutations. The WorkPlan contains the code/project impact analysis and selected tests, sets `requiresAuthoring=true`, and intentionally has no executable steps. Use `plan-amend` to add reviewed mutations. `plan-submit` already rejects a plan with zero executable steps.

Recommended agent flow:

1. `reindex`;
2. `code-impact` or `plan-from-diff`;
3. inspect medium/high-confidence source mappings;
4. `test-selection`;
5. inspect affected Requirement/Business Rule/API/DB/Test entities;
6. amend the draft WorkPlan with deterministic steps;
7. submit/review/approve/execute through the normal WorkPlan lifecycle.
