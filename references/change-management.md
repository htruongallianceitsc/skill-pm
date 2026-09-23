# Change Management, Audit, Review, and Baselines

## Goals

Keep project data local-first while making AI/user edits reviewable and reversible. Store audit artifacts under `.pm/`; never turn change sets into domain entities or use them as relation targets.

## Storage

```text
.pm/
├── change-management.json
├── audit-state.json
├── changesets/
│   └── CHG-*.json
└── baselines/
    └── <name>.json
```

A change set stores full before/after records for every touched entity. Each record includes the entity JSON plus referenced UTF-8 content files and a payload hash. This permits entity-field diffing, referenced-file diffing, stale-state detection, and local undo.

## Automatic audit

CLI mutations (`create`, `update`, `link`, `unlink`, `transition`, `delete`, `restore`, and pulled changes from `sync`) compare the workspace before and after the operation and write one applied change set for the command.

Use `--actor`, `--reason`, and repeatable `--related` metadata when available. Prefix AI actor ids with `ai:`. Use `PM_ACTOR` as a convenient default actor environment variable.

For direct editor/manual changes that bypass the CLI, run:

```bash
python scripts/pm_project.py audit-scan --project ./Project
```

`audit-scan` compares the live workspace with `.pm/audit-state.json`, records any changed entity/referenced text payload, and advances the audit state. Use `--initialize` only when adopting audit in an existing workspace and the current state should become the starting point without generating historical changes.

## AI proposal/review workflow

For an AI change that requires human approval, do not modify the live entity directly. Create a pending JSON Merge Patch proposal:

```bash
python scripts/pm_project.py propose \
  --project ./Project \
  --ref REQ-001 \
  --patch-json '{"data":{"priority":"high"}}' \
  --actor ai:agent \
  --reason "Raise priority after impact review"
```

Then use `reviews` and `review-show`. Approval verifies that the live payload hash still equals the proposal's `beforeHash`; if it changed, approval stops and requires a new review. `review-approve` applies the proposal, validates the whole project, and rolls back if validation fails. `review-reject` changes no domain data.

Proposal patches may not change `uid`, `entityType`, `localRef`, `id`, `code`, or `createdAt`.

## History, diff, and undo

- `history --ref REQ-001`: list change sets touching an entity.
- `diff --ref REQ-001`: show the latest audited before/after entity and referenced-file diff.
- `diff --ref REQ-001 --change-set CHG-...`: inspect a specific change.
- `diff --ref REQ-001 --baseline release-1.2`: compare a baseline snapshot to the current entity.
- `undo --change-set CHG-...`: reverse an applied change set only when every current payload still matches its recorded `afterHash`.

Use `--force` undo only after reviewing newer changes. Never physically remove an entity that already has locked server identity; use normal soft-delete/server workflows instead.

## Baselines

Create named snapshots at releases, UAT handoff, or before large migrations:

```bash
python scripts/pm_project.py baseline-create --project ./Project --name release-1.2 --release 1.2.0
python scripts/pm_project.py baseline-compare --project ./Project --from release-1.2 --to current
```

A baseline stores full entity/referenced-text records plus a manifest hash. Treat an existing named baseline as immutable by convention; replacing it requires explicit `--force`.

## Portable bundle

`bundle-export` includes change sets, baselines, and audit state in `changeManagement[]`. `bundle-import` restores only whitelisted `.pm/changesets/`, `.pm/baselines/`, and `.pm/audit-state.json` paths.

## Limits

Binary referenced files are hashed but are not restorable through audit snapshots or portable bundle import. Keep large/binary assets outside this change-management mechanism or manage them through a dedicated artifact store.
