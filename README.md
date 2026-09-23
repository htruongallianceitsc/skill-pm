# Project Workspace Manager

A Claude Code skill for managing schema-driven software projects stored as **portable local folders of JSON entities** plus optional Markdown, HTML, SQL, OpenAPI, and test files.

The project folder is the source of truth. Normal CRUD works fully offline; the Project Manager API is used only for collaboration, remote viewing/editing, identity assignment, and synchronization.

## Install

```bash
claude plugin marketplace add htruongallianceitsc/skill-pm
claude plugin install project-workspace-manager@skill-pm
```

Or from a local clone:

```bash
claude plugin marketplace add /path/to/skill-pm
claude plugin install project-workspace-manager@skill-pm
```

Verify:

```bash
claude plugin details project-workspace-manager@skill-pm
```

## What it does

- Initialize and maintain project workspaces from a schema registry (`.pm/entity-types.json`)
- Create, edit, query, link, and transition entities with lifecycle and schema validation
- Relationship mapping, traceability, and coverage analysis driven by `.pm/relation-map.json` and `.pm/quality-rules.json`
- Bounded graph traversal for focused AI context, impact analysis, orphan detection, and health checks
- AI-authored WorkPlans with review / approval / execution (`.pm/agent-workflow.json`, `.pm/workplans/`)
- Audited change history, diff/undo, release baselines, and portable bundles
- Sync through one Project Manager API with immutable server `id`/`code` fields and explicit conflict handling

## Repository layout

```
.claude-plugin/
  marketplace.json      # marketplace manifest (repo doubles as its own marketplace)
  plugin.json           # plugin manifest
SKILL.md                # the skill (discovered at plugin root)
references/             # detailed reference docs loaded on demand
scripts/pm_project.py   # stdlib-only local-first workspace CLI
assets/project-template/# scaffold copied when initializing a workspace
agents/openai.yaml      # ChatGPT/Codex app manifest (not used by Claude Code)
```

## Reference docs

| File | Topic |
|---|---|
| `references/architecture.md` | Overall design |
| `references/core-data-model.md` | Entity envelope and identity rules |
| `references/entity-catalog.md` | Starter entity types |
| `references/schema-driven-runtime.md` | Generic client/UI contract |
| `references/relationship-mapping.md` | Relation map semantics |
| `references/quality-traceability.md` | Coverage and quality rules |
| `references/project-intelligence.md` | Graph context, impact, health |
| `references/agent-workflow.md` | WorkPlan review/approval/execution |
| `references/change-management.md` | Audit, diff, undo, baselines |
| `references/portable-bundle.md` | Export/import bundles |
| `references/sync-protocol.md` | Sync and conflict handling |
| `references/api_reference.md` | Project Manager API surface |

## Requirements

- Claude Code
- Python 3 (standard library only) for `scripts/pm_project.py`
