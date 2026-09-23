---
name: project-workspace-manager
description: Manage documentation-first, schema-driven software projects stored as portable local folders of JSON entities plus Markdown/HTML/source/test files. Use for bootstrapping an idea into structured documents; tracing every project request/question; grouping Features under Modules; generating offline HTML entity graphs and business/feature diagrams; local search/query/doctor; source/Git dependency and impact analysis; reviewed WorkPlans; explicit implementation with Task status updates; audited history, baselines, portable bundles, and optional API sync.
---

# Project Workspace Manager

Treat the project folder as the portable local source of truth. Keep normal local CRUD fully usable without the Project Manager API. Use the API only for collaboration, remote viewing/editing, identity assignment, and synchronization.

## Operating model

1. Read `project.json`.
2. Read `.pm/config.json` and `.pm/entity-types.json` before modifying entities.
3. Treat `.pm/entity-types.json` as the registry and UI/lifecycle definition. Never hard-code the starter type list.
4. Read each type's `dataSchema` before creating or editing `data`.
5. Read `.pm/lookups.json` for reusable option lists.
6. Read `.pm/relation-map.json` before changing relationships. Never infer a relationship from convention.
7. Use `.pm/quality-rules.json` for traceability/coverage checks. Do not hard-code quality expectations.
8. Use `.pm/intelligence.json` for bounded graph traversal, context, impact, orphan, and health behavior.
9. Use `.pm/local-engine.json`, `.pm/views.json`, and `.pm/indexes/search-index.json` for local indexed discovery/query/view behavior. Treat indexes as disposable derived caches, never source data.
10. Use `.pm/documentation-policy.json` and `.pm/requests/` for documentation-first interaction tracing, implementation gating, and generated HTML graph behavior.
11. Use `.pm/source-intelligence.json` and `.pm/indexes/source-index.json` for source-code/Git introspection. Keep source index disposable; keep domain entities as the durable project model.
12. Use `.pm/agent-workflow.json` and `.pm/workplans/` for reviewed AI planning; never treat a WorkPlan as a domain entity.
13. Use `uid` as the permanent internal key for all relations.
14. Leave official `id` and `code` null for locally created records until the API assigns them.
15. Once server identity is locked in `.pm/sync-state.json`, never change `id` or `code` locally.
16. Keep sync bookkeeping outside domain entity JSON.
17. Prefer soft deletion. Physically purge only after remote acknowledgement and an explicit user request.
18. Rebuild manifest/indexes after semantic changes; rebuild the source index after source-code/config changes when Git/source analysis is needed.

## Documentation-first interaction workflow

Apply this workflow to every project-scoped user question or request unless the user explicitly asks not to persist it:

1. Capture the interaction first with `request-capture`. Questions are traced even when they do not create a document. Idea/request/requirement/change records create a Request Note document by default.
2. When the workspace is empty and the user starts from an idea, run `idea-bootstrap` before designing code. It creates Idea Brief, Product Scope, Functional Overview, Business Rules & Assumptions, Technical Outline, and Open Questions/Decisions documents.
3. Update/create documents before implementation planning. Keep business/process/feature understanding in Markdown or standalone HTML documents.
4. Treat Module strictly as a functional group of Features (`Feature --belongs_to--> Module`). Example: `AUTH` groups Login/Register/Forgot Password. Do not use Module as a catch-all parent for API/Requirement/Test entities.
5. Never execute implementation with `plan-execute` directly when the default policy is active. Use the explicit `implement` command only after the WorkPlan is approved and documentation is linked.
6. Let `implement` transition related Tasks to `in_progress` and then `done` after success; if no Task is linked, allow the policy to create an implementation Task automatically.
7. Regenerate `docs/project-graph.html` automatically whenever manifest/indexes rebuild. Use `graph-build` to force regeneration after external/manual edits.
8. Use `diagram-create` to generate portable HTML flow/business-flow/feature-flow/architecture documents. Prefer `--from-ref` for a quick feature map; use `--spec-file` when the actual sequence/business flow matters.

See `references/documentation-first.md`.

## Schema-driven entity workflow

For a new entity:

1. Resolve the type in `.pm/entity-types.json`.
2. Generate a globally unique `uid`.
3. Set `id` and `code` to null.
4. Set `localRef` to `local:<entityType>:<short-uid>`.
5. Apply registry defaults.
6. Merge user data under `data`.
7. Validate `data` against the type's `dataSchema`.
8. Use the lifecycle `initialStatus` unless the user explicitly requests another valid status.
9. Save to the configured folder using `<uid>.json`.
10. Rebuild indexes.

For an edit:

- Preserve `uid`, `entityType`, `localRef`, and locked server identity.
- Put type-specific values under `data`.
- Increment `revision` exactly once per semantic edit.
- Update `updatedAt`.
- Validate schema and relations before completing the operation.

For status changes:

- Use the type's `lifecycle.statuses` as the valid set.
- Follow `lifecycle.transitions` by default.
- Bypass a transition only when the user explicitly requests a forced transition; the target status must still be configured.

See `references/schema-driven-runtime.md` for the generic client/UI contract.

## Relationship workflow

Before adding or removing a relation:

1. Resolve source and target by `uid`.
2. Read `.pm/relation-map.json`.
3. Match source type, relation type, and target type. Respect `*` wildcards.
4. Enforce cardinality, required rules, and unresolved-target policy.
5. Store only the outgoing relation on the source entity using `targetUid`.
6. Use the generated relation index for reverse lookup; do not duplicate reverse relations into target files.
7. Rebuild the relation index.

Never use server `id`, `code`, or filesystem paths as relation keys.

## Content workflow

Use `content[]` for long-form or executable content:

- Use inline mode for small Markdown, HTML, text, or structured content that benefits from a self-contained entity JSON.
- Use file mode for developer-edited scripts/specs or larger content.
- Keep referenced paths project-relative and inside the project root.
- Include file content/hash in sync only when supported by configuration.

## Local search, query, views, and doctor

Treat local indexes as rebuildable acceleration only. Entity JSON and referenced project files remain canonical.

1. Use `reindex` after manual/bulk filesystem edits; normal CLI semantic commands rebuild indexes automatically.
2. Use `search` for full-text discovery across core metadata, `data.*`, inline content, and referenced UTF-8 content. Allow it to rebuild stale indexes unless the caller explicitly requests `--no-reindex`.
3. Use `query --expr` for field-aware filtering with boolean expressions and optional graph scope (`--related-to`, relation, direction, depth). Prefer this over ad-hoc file scans.
4. Keep reusable local dashboards in `.pm/views.json`; run them with `view-run`. Saved views store only query/presentation configuration, never copied entity data.
5. Use `doctor` to diagnose structural errors, broken links, stale/missing derived indexes, and invalid saved-view expressions.
6. `doctor --fix` may only repair safe local infrastructure by default. Require explicit `--fix-level semantic` for deterministic business-file repairs such as correcting a relation's `targetType` to the actual target entity type when the configured relation map allows it. Audit semantic doctor repairs as ChangeSets.
7. Do not hide validation errors by deleting or weakening source data/rules during auto-fix.

See `references/local-search-query.md` and `references/project-doctor.md`.

## Quality and traceability

Use `.pm/quality-rules.json` to express coverage expectations such as requirement-to-test coverage, feature-to-requirement traceability, or bug/task linkage.

- Run `quality` after meaningful batches of changes and before releases/reviews.
- Treat findings as configurable policy, not schema corruption.
- Sync is blocked only at or above the severity configured by `validation.qualityRulesBlockSyncAtSeverity`.
- Keep generated reports under `.pm/reports/`; reports are disposable derived data.

See `references/quality-traceability.md`.


## Project intelligence workflow

Before substantial work on a known entity:

1. Resolve it with `context --detail summary`; never guess when a code/title is ambiguous.
2. Use the configured `context` graph profile to load only nearby entities first.
3. Run `impact` before edits that can affect linked requirements, screens, APIs, database objects, tests, tasks, or bugs. Treat results as potential impact based on graph evidence, not guaranteed runtime impact.
4. Load full referenced Markdown/HTML/API/SQL/test content only when exact details are needed.
5. After edits run `validate`, then `quality`/`coverage` as appropriate.
6. Use `orphans`, `broken-links`, `missing-tests`, and `health` for diagnostics and release/review preparation.

Keep traversal semantics in `.pm/intelligence.json`; keep edge validity in `.pm/relation-map.json`; keep quality expectations in `.pm/quality-rules.json`. Do not encode a fixed Feature/Requirement/Test hierarchy in agent reasoning.

See `references/project-intelligence.md`.

## Change management and review workflow

Treat `.pm/changesets/` and `.pm/baselines/` as canonical project governance records, separate from domain entities. Use `.pm/audit-state.json` only as local detection state.

For normal CLI edits:

1. Supply `--actor`, `--reason`, and optional repeated `--related` references when useful.
2. Let the CLI snapshot the workspace before/after the semantic command and create one applied ChangeSet containing every touched entity plus referenced UTF-8 content files.
3. Run `audit-scan` after direct filesystem/editor changes that bypass the CLI.

For AI changes requiring approval:

1. Use `propose` with a JSON Merge Patch; do not edit the live entity first.
2. Use `reviews`/`review-show` to present exact field changes.
3. Approve only through `review-approve`; stale proposals must fail when the live payload no longer matches the reviewed base hash.
4. Use `review-reject` with a reason when rejecting.

For recovery/release management:

- Use `history` and `diff` for audit investigation.
- Use `undo` only when current payloads still match the ChangeSet's recorded after hashes; force only after reviewing newer changes.
- Create named baselines at releases, UAT handoff, or before major migrations and use `baseline-compare` to inspect drift.
- Never physically undo-create a server-assigned entity; use normal soft-delete/server workflows.

See `references/change-management.md`.

## Agent WorkPlan workflow

Use WorkPlans for substantial AI-driven changes, especially requests that may touch multiple requirements, screens, APIs, database objects, tests, tasks, or files. Keep planning/review separate from mutation.

1. Investigate first with `query`, `describe`, `context`, `impact`, and optionally `plan-context`; do not invent entities or relations that configuration does not support.
2. Author a deterministic JSON plan spec with assumptions, risks, acceptance criteria, related Task/Bug references, and ordered/dependent steps.
3. Use `plan-create`, then `plan-validate`.
4. Use `plan-submit` to capture base entity hashes and the workspace-definition hash.
5. Present `plan-show` for review; approve with `plan-approve` or reject with `plan-reject`.
6. If project data/config changed after submission, approval/execution must stop. Use `plan-refresh`, amend while draft with `plan-amend`, and review again.
7. Use `plan-execute` only after approval unless project policy explicitly disables the approval gate. Resolve `$step:<id>` and `$handle:<name>` only from earlier create outputs.
8. Execute all steps as one logical transaction. On failure, restore entity/referenced-file snapshots and mark the plan failed.
9. After successful execution, create exactly one ChangeSet linked by `workPlanId`; preserve execution step outputs and `changeSetId` in the WorkPlan.
10. By default, block on newly introduced quality findings at or above the configured severity rather than unrelated legacy findings.

Supported starter operations are `create`, `update`, `content-upsert`, `link`, `unlink`, `transition`, `delete`, and `restore`. Keep allowed operations in `.pm/agent-workflow.json`; never bypass relation/lifecycle/schema rules just because a plan was AI-authored.

WorkPlans sync bidirectionally through the same Project Manager endpoint and use optimistic conflict handling separate from domain entity sync. See `references/agent-workflow.md`.

## Local source and Git intelligence

Treat source-code analysis as local evidence layered on top of the project knowledge graph. Do not create one domain entity per source file by default.

1. Configure scan roots/extensions/ignores and explicit mappings in `.pm/source-intelligence.json`.
2. Run `scan-source` to build `.pm/indexes/source-index.json`. The index stores file hashes, languages, lightweight symbols/imports, and evidence that maps source paths back to project entities.
3. Prefer exact mapping evidence from entity JSON/content/data paths. Use technical identifiers such as API path/operationId or table name as medium-confidence evidence. Use explicit path mappings for ambiguous codebases.
4. Use `source-map` to inspect why a file maps to an entity before relying on the mapping for impact analysis.
5. Use `git-status`, `changes-since-commit`, `git-impact`, and `commit-context` to translate Git file changes into direct entity matches and graph-based potential impact. Treat graph expansion as potential impact, not proof of runtime behavior.
6. Use `git-link` to attach a real commit to an existing applied ChangeSet. Use `git-changeset` only when a commit needs evidence/audit capture but no entity before/after snapshot exists; evidence ChangeSets are not reversible.
7. Use `scan-openapi`, `scan-database`, and `scan-playwright` without `--apply` for discovery first. Add `--apply` only when the generated candidates should become API/database-table/test-script entities.
8. Use `import-markdown` to turn an existing Markdown file into a document entity while preserving a project-relative content reference.
9. Never invent links merely because filenames look similar. Preserve mapping evidence and confidence.

See `references/git-source-introspection.md`.

## Code dependency and change intelligence

Use the disposable code dependency graph to reason from changed files to dependent code before crossing into the durable project graph.

1. Build source/dependency indexes with `reindex`, or inspect one path with `code-deps --rebuild`.
2. Use `code-deps` / `dependency-tree` to inspect imports and dependents; use `circular-dependencies` for strongly connected source cycles.
3. Use `code-impact` for working-tree, commit, range, or explicit-path analysis. Traverse source **dependents** first, then map files to entities, then expand via configured project impact relations. Treat all results as potential impact.
4. Use `test-selection` or Git-oriented `changed-tests` to select regression tests from both source dependencies and `test-script` entities. Return runner commands only when the workspace has enough evidence; never invent a test runner.
5. Use `plan-from-git` / `plan-from-diff` to create a draft WorkPlan shell containing impact evidence and selected tests. These generated plans intentionally have zero executable steps and `requiresAuthoring=true`; amend them with reviewed deterministic steps before submission.
6. Configure path aliases and test globs under `.pm/source-intelligence.json`; keep `.pm/indexes/dependency-index.json` disposable.

See `references/code-change-intelligence.md`.

## Sync workflow

Use the single endpoint configured in `.pm/config.json`. Sync both domain entities and the workspace definition needed by a generic web client.

Normal sync:

1. Run structural/schema validation.
2. Generate the quality report and apply configured blocking severity.
3. Rebuild manifest/indexes.
4. Compute changed entity hashes relative to `.pm/sync-state.json`.
5. Compute the workspace-definition hash from entity registry, relation map, lookups, quality rules, intelligence profiles, and schemas.
6. Apply optional scope filters for entity types and/or UIDs.
7. Send one sync request with entity changes, WorkPlans, audit/baseline records, cursor, and changed workspace definition.
8. Apply first-time server `id`/`code`; lock them in sync state.
9. Never rewrite a locked server identity silently.
10. Pull remote changes only when the corresponding local object is clean relative to its last synced hash.
11. If both sides changed, write conflict snapshots and keep the live local copy unchanged.
12. Update cursor/hash/version state only after successful processing.
13. Rebuild manifest/indexes again.

Conflict resolution:

- Default to manual review.
- Show local and remote versions.
- If the user explicitly chooses local, sync only the intended entity/definition where practical using force-local.
- Send the server conflict token so the force applies to the exact remote version reviewed.
- If the token is stale, stop and require review of the newer remote version.

See `references/sync-protocol.md` and `references/api_reference.md`.

## Deterministic CLI

Use `scripts/pm_project.py` for repeatable operations:

- `init`: initialize a workspace from the template.
- `idea-bootstrap`: initialize an empty workspace with a structured documentation pack for an idea, without generating code.
- `request-capture` / `request-list` / `request-show` / `request-close`: persist interaction trace records and documentation links.
- `graph-build`: regenerate the standalone offline HTML entity/relation graph.
- `diagram-create`: create a Document backed by a standalone HTML flow/feature/business diagram.
- `implement`: the explicit documentation-gated WorkPlan execution command; automatically drives implementation Task status.
- `create`: create a schema-valid local entity.
- `update`: update title, tags, or type-specific data without touching identity/status.
- `transition`: change status through the configured lifecycle.
- `link` / `unlink`: mutate validated relations.
- `delete` / `restore`: soft-delete or restore.
- `query`: filter local entities with boolean field expressions and optional graph scope; legacy type/status/tag/text flags remain supported.
- `search`: use the local inverted index to search metadata, `data.*`, inline content, and referenced UTF-8 files.
- `reindex`: rebuild manifest, entity/relation/search indexes, plus the local source-code index.
- `view-list` / `view-show` / `view-run`: execute reusable saved local views from `.pm/views.json`.
- `doctor`: diagnose workspace/index/view/link problems; optionally apply safe or explicit semantic repairs.
- `describe`: return an entity type definition, schema, lookups, and relation mappings for an agent/generic client.
- `manifest`: rebuild generated indexes.
- `validate`: validate identity, schema, lifecycle status, relations, files, and server locks.
- `quality`: generate traceability/coverage findings and per-rule coverage metrics.
- `trace`: traverse a configured graph profile from one entity.
- `context`: build a bounded AI context pack around one entity.
- `impact`: list potentially related/affected entities using the configured impact profile.
- `coverage`: inspect categorized quality/coverage rules globally or for one entity.
- `orphans`: list isolated entities after configured exclusions.
- `broken-links`: find invalid, missing, deleted, or mismatched relation targets.
- `missing-tests`: convenience view of failed `test-coverage` rules.
- `health`: summarize validation, quality, graph, identity, and sync metrics without an opaque score.
- `history` / `diff`: inspect audited entity and referenced-file history.
- `propose` / `reviews` / `review-show` / `review-approve` / `review-reject`: stage and review AI/user changes without touching live data until approval.
- `undo`: reverse an applied ChangeSet with stale-state protection.
- `audit-scan`: detect direct/manual edits that bypassed the CLI.
- `baseline-create` / `baseline-list` / `baseline-compare`: manage release/UAT/migration snapshots.
- `plan-context`: collect bounded context/impact inputs for an AI planning pass.
- `plan-create` / `plan-amend` / `plan-validate`: author and refine deterministic WorkPlans without mutating live entities.
- `plans` / `plan-show`: inspect planning state and stale-context findings.
- `plan-submit` / `plan-refresh` / `plan-approve` / `plan-reject` / `plan-cancel`: run the WorkPlan review lifecycle with base-hash protection.
- `plan-execute`: execute an approved plan atomically and create one linked ChangeSet.
- `scan-source` / `source-map`: build and inspect source-file-to-entity evidence without turning source files into domain entities.
- `code-deps` / `dependency-tree` / `circular-dependencies`: inspect the disposable local source dependency graph.
- `code-impact`: propagate changed files through source dependents, then map into durable project-graph potential impact.
- `test-selection` / `changed-tests`: select regression tests using both source dependencies and project `test-script` impact.
- `plan-from-git` / `plan-from-diff`: create reviewable draft WorkPlan shells from code-change evidence; amend before submission.
- `git-status` / `changes-since-commit` / `git-impact` / `commit-context`: connect Git changes to entity context and graph-based potential impact.
- `git-link` / `git-changeset`: associate commits with audited project changes or capture non-reversible Git evidence.
- `scan-openapi` / `scan-database` / `scan-playwright`: introspect technical artifacts and optionally create domain entities with `--apply`.
- `import-markdown`: import an existing Markdown file as a portable document entity.
- `bundle-export`: export the entire portable project into one self-contained JSON bundle for browser/desktop clients.
- `bundle-import`: restore a workspace folder from the portable JSON bundle.
- `sync`: call the configured single sync endpoint; supports scope filters, dry-run, changed workspace definitions, and explicit force-local mode.

Run `validate` before sync and after bulk/manual edits.

## Reference files

- `references/architecture.md`: folder layout, sources of truth, portability, and extension model.
- `references/core-data-model.md`: common entity envelope and identity/content lifecycle.
- `references/schema-driven-runtime.md`: how a generic client generates CRUD UI and behavior from JSON configuration.
- `references/entity-catalog.md`: starter entities and their default responsibilities.
- `references/relationship-mapping.md`: dynamic graph rules and cardinality.
- `references/quality-traceability.md`: configurable coverage/traceability rules.
- `references/project-intelligence.md`: bounded context retrieval, trace, impact, coverage, orphan/broken-link diagnostics, and health reporting.
- `references/local-search-query.md`: local full-text index, query expression language, graph-scoped filtering, and saved views.
- `references/project-doctor.md`: diagnostic categories, safe vs semantic repairs, and audit expectations.
- `references/git-source-introspection.md`: source indexing, importers, Git mapping evidence, commit context, and Git-to-graph impact workflow.
- `references/change-management.md`: audited ChangeSets, AI proposal/review, diff/undo, direct-edit detection, and release baselines.
- `references/agent-workflow.md`: AI WorkPlan lifecycle, stale-context protection, deterministic operations, atomic execution, and planning sync.
- `references/sync-protocol.md`: sync protocol, conflicts, workspace-definition sync, and server identity.
- `references/api_reference.md`: endpoint contract and implementation guidance.
- `references/portable-bundle.md`: single-JSON bundle contract for offline browser/desktop clients.
