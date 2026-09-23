# Agent Workflow and WorkPlans

## Purpose

Use WorkPlans to separate **reasoning/planning** from **project mutation**. An AI agent may investigate and author a plan freely, but project entities are changed only after the WorkPlan passes validation/review and is explicitly executed.

WorkPlans are governance records under `.pm/workplans/`; they are not domain entities and do not participate in the normal relation graph. A completed WorkPlan points to the single ChangeSet produced by its execution.

## Lifecycle

```text
draft
  -> pending-review
      -> approved
          -> executing
              -> completed
              -> failed
      -> rejected
  -> cancelled
```

`plan-refresh` returns a non-terminal/non-executing plan to `draft`, refreshes its base hashes, and invalidates prior approval so it can be reviewed again.

## Identity and stale-context protection

`planId` is generated locally and remains permanent. Remote database identity/version belongs in `.pm/sync-state.json`, not in the WorkPlan document.

At `plan-submit`, capture:

- hashes of every existing entity referenced by `contextRefs`, `sourceRef`, or `targetRef`;
- the current workspace-definition hash.

`plan-approve` and `plan-execute` compare these base hashes with the live workspace. If referenced entities or schemas/mappings changed, stop and require refresh/re-review. Use `--force-stale` only after explicit human review of the newer state.

## WorkPlan shape

A typical plan spec authored by an AI looks like:

```json
{
  "assumptions": ["Email delivery already exists"],
  "risks": ["Reset token expiration must be enforced"],
  "acceptanceCriteria": ["Reset flow works end to end"],
  "contextRefs": ["MOD-AUTH"],
  "related": ["TASK-123"],
  "steps": [
    {
      "stepId": "S1",
      "operation": "create",
      "entityType": "feature",
      "handle": "reset-feature",
      "payload": {
        "title": "Reset password",
        "data": {"priority": "high"}
      }
    },
    {
      "stepId": "S2",
      "operation": "link",
      "sourceRef": "$handle:reset-feature",
      "relation": "belongs_to",
      "targetRef": "MOD-AUTH"
    }
  ]
}
```

The complete stored document also contains lifecycle status, actor/reason, base hashes, events, and execution results. `schemas/core/workplan.schema.json` is synchronized with the workspace definition so a generic web client can render/edit plans.

## Symbolic references

Use symbolic references for entities created earlier in the same plan:

- `$step:S1` — output entity created by step `S1`;
- `$handle:reset-feature` — output entity from the create step declaring `"handle":"reset-feature"`.

Symbolic dependencies are added automatically to execution ordering. Explicit `dependsOn` is still useful when a step depends on another step for reasons other than an entity reference.

Never invent a future server `id` or `code` for a create step. The create step yields a local `uid`; server identity is assigned later by normal sync.

## Supported deterministic operations

The starter executor supports:

- `create`: create a schema-driven local entity; do not provide identity fields or relations in `payload`;
- `update`: JSON Merge Patch mutable entity fields; use `transition` for status and `delete/restore` for deletion state;
- `content-upsert`: add/replace inline text or a UTF-8 file under `files/<entityType>/<uid>/`;
- `link` / `unlink`: mutate a relationship allowed by `.pm/relation-map.json`;
- `transition`: apply a configured lifecycle transition;
- `delete` / `restore`: soft-delete or restore.

Keep the operation list configurable in `.pm/agent-workflow.json`. Add new deterministic operations only when their validation and rollback semantics are clear.

## Recommended AI workflow

For a request such as “add reset password”:

1. Use `query`, `describe`, `context`, and `impact` to identify relevant existing scope.
2. Optionally run `plan-context` to collect bounded context packs around known references.
3. Author a JSON plan spec with assumptions, risks, acceptance criteria, related Task/Bug references, and deterministic steps.
4. Run `plan-create` and `plan-validate`.
5. Run `plan-submit`; this freezes base hashes and moves the plan to `pending-review`.
6. Present `plan-show` to the user/reviewer.
7. Run `plan-approve` or `plan-reject`.
8. Run `plan-execute` only after approval.
9. Inspect the resulting ChangeSet, then use `validate`, `quality`, `coverage`, and `health` as appropriate.
10. Sync through the single Project Manager endpoint.

If review feedback requires changing the plan, use `plan-refresh` when needed and `plan-amend` while the plan is `draft`/`failed`, then submit it again.

## Atomic execution and ChangeSets

Before execution, snapshot all current entity payloads and referenced UTF-8 content. Execute steps in dependency order. After all steps:

1. validate the complete project;
2. run quality rules when enabled;
3. by default block only **new** findings at or above `execution.blockAtQualitySeverity` (`qualityMode: no-new-blocking`), so legacy problems do not prevent unrelated work;
4. create one ChangeSet containing every entity/file touched by the plan;
5. store the resulting `changeSetId` in `WorkPlan.execution`.

If a step/validation/quality check fails, restore the before snapshots and mark the WorkPlan `failed`. This is process-level atomic rollback; a hard process/machine crash still requires normal filesystem recovery/audit scanning.

## Planning sync

WorkPlans use the same `/api/project-manager/sync` call as entities, definitions, ChangeSets, and baselines. The request carries changed plans under:

```json
{
  "planning": {
    "workPlans": [
      {
        "planId": "PLAN-...",
        "baseRemoteVersion": 3,
        "payloadHash": "sha256:...",
        "conflictToken": null,
        "document": {}
      }
    ]
  }
}
```

The server can acknowledge them in `workPlansApplied`, send web-edited plans in `remoteWorkPlans`, or report `workPlanConflicts`. The client uses the same optimistic-concurrency rule as entities: auto-apply a remote plan only when the local plan is clean relative to its last synced hash; otherwise store local/remote conflict snapshots. Explicit `forceLocal` should carry the reviewed conflict token.

## Generic web client

A generic Project Manager web app should expose WorkPlans as a separate planning/review surface, not as business entities. It can use:

- `.pm/agent-workflow.json` for lifecycle/policy;
- `schemas/core/workplan.schema.json` for editing/validation;
- `planning.workPlans` sync payloads for persistence;
- `execution.changeSetId` to navigate from an approved plan to the exact changes it produced.
