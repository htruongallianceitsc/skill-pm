# Project Consistency & Governance

## Purpose

Keep requests, decisions, documentation, project entities, tests, and implementation status consistent as the project evolves. All behavior is local-first and derived reports can be rebuilt from project files.

## Request promotion

`request-capture` is an interaction log, not automatically a Requirement. Use:

- `request-analyze` to inspect the trace and allowed promotion targets.
- `request-link` to attach existing entities without creating duplicates.
- `request-promote` only when the interaction becomes durable project knowledge/work.

Typical flow:

```text
Request / Question
  -> analysis
  -> Decision / Requirement / Feature / Bug / Task / Document
  -> WorkPlan
  -> implementation
```

Questions should normally become a Decision only after the answer is durable enough to preserve. Resolving a Decision linked to a request closes the source question trace.

## Decision lifecycle

Starter lifecycle:

```text
proposed -> accepted -> superseded
         -> rejected
```

Never rewrite an accepted Decision to represent a new policy. Create a replacement and use `decision-supersede` so history and rationale remain visible.

Decision data includes context, decision text, rationale, consequences, requestId, decidedBy, and decidedAt. Use relations to connect the Decision to Feature/Requirement/Rule/API/other affected scope.

## Documentation freshness

A Document becomes freshness-trackable when it has outgoing `documents` relations to durable project entities.

`doc-reconcile` records the current source entity `uid`, `revision`, and payload hash under `data.freshness.sources`. Later changes to a linked entity make the document stale even when its filename/content path did not change.

Statuses:

- `untracked`: no reconciled source snapshot exists.
- `current`: all recorded revisions/hashes match.
- `stale`: at least one source changed, disappeared, or lacks a matching snapshot.
- `waived`: reviewer explicitly accepted freshness debt with a note.

Do not use `doc-reconcile` as a substitute for editing incorrect documentation. Update/review content first, then reconcile. During `implement`, a linked untracked document gets a pre-change baseline. If that document is also changed by the WorkPlan, the post-implementation snapshot is refreshed automatically. Otherwise source changes leave it stale.

## Definition of Ready

Default checks:

1. WorkPlan is approved.
2. Relevant documentation exists.
3. No open question trace is linked to the implementation scope.
4. Workspace structural validation is clean.

Use `ready-check` for inspection. `implement` enforces it unless an explicit waiver is supplied after review.

## Definition of Done

Default checks:

1. Implementation Tasks are complete (or still `in_progress` during the pre-close gate).
2. Structural validation is clean.
3. No blocking quality errors exist.
4. Linked documentation is current, unless explicitly waived.

If execution succeeds but Done fails, do not rollback successful implementation merely because documentation is stale. Keep/mark implementation Tasks blocked, fix/reconcile the docs or other gate, then use `implementation-complete` to close Tasks. A failed WorkPlan execution itself remains atomic and rolls back as before.

## Traceability matrix

`traceability` generates `.pm/reports/traceability.json` and `docs/traceability.html`. Feature-centered rows include:

- Module
- Feature
- Requirements
- Business Rules
- Screens
- APIs
- Database tables
- Test cases and test scripts
- Documents
- Decisions
- Tasks and bugs

The report is derived; do not edit it as source data.

## Dashboard

`dashboard-build` writes `.pm/reports/dashboard.json` and `docs/project-dashboard.html`. It summarizes entity inventory, open requests/questions/decisions, document freshness, quality findings, traceability gaps, and recent ChangeSets. It links to `project-graph.html` and `traceability.html` and works offline without CDN dependencies.
