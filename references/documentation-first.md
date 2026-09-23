# Documentation-first workflow

## Core rules

1. Trace every project-scoped user question/request with `request-capture` before doing project work.
2. For idea/change/request/requirement work, create or update Documents first. `request-capture` creates a Request Note by default.
3. Never implement code by calling `plan-execute` directly. Use `implement` only after an approved WorkPlan and related documentation exist.
4. `implement` sets related Tasks to `in_progress`, executes the WorkPlan, then marks those Tasks `done`; if no Task exists it creates one by default.
5. Use `idea-bootstrap` on a new/empty workspace to create a documentation pack before entities or code are designed.
6. `docs/project-graph.html` is generated from current entities/relations every time manifest/indexes are rebuilt.
7. Use `diagram-create` for standalone HTML business/feature/process diagrams. A JSON spec uses `nodes[]`, `edges[]`, optional `direction`, and `notes`. `--from-ref` creates a quick feature/entity map from the knowledge graph.

## Example diagram spec

```json
{
  "title": "Login flow",
  "direction": "LR",
  "nodes": [
    {"id":"user","label":"User","type":"actor"},
    {"id":"form","label":"Login form","type":"screen"},
    {"id":"api","label":"POST /login","type":"api"},
    {"id":"result","label":"Session created","type":"result"}
  ],
  "edges": [
    {"from":"user","to":"form","label":"Enter credentials"},
    {"from":"form","to":"api","label":"Submit"},
    {"from":"api","to":"result","label":"Valid"}
  ],
  "notes": "Invalid credentials return an error and no session."
}
```
