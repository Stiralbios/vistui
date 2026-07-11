---
description: Update all agents files
agent: build
---

Update AGENTS.md and all agent files in `.opencode/agents/` so they accurately reflect the current project state.

### Task Overview

You are updating the developer-facing agent instructions for the **Accurial** project. These files tell automated agents how to behave when working on the repository. They must stay in sync with the actual code, Makefile, and project documentation.

### Files to Update

1. `AGENTS.md` (repo root)
2. `.opencode/agents/* (all other agents)

### Sources of Truth

To determine what each agent file should contain, examine the following (they may have drifted from the agents):

- `Makefile` — commands, targets, and aliases
- `documentations/architecture/` — backend-structure, data-flow, schema-strategy, database, overview
- `documentations/development/backend/` — conventions, adding-a-feature
- `documentations/backend/` — testing, error-handling, features, auth, user
- `documentations/frontend/` — setup
- `documentations/api/` — openapi
- `sources/backend/` — actual code structure, model naming, layer patterns
- `tests/backend/` — test fixtures, factory patterns

### Update Rules

- **Do not invent information.** If something is unclear, read the relevant source file rather than guessing.
- Preserve the existing tone and structure of each agent file unless it has become misleading.
- Make the agents **concise but complete**: include critical conventions, naming, layer rules, error handling, commands, and paths.
- Ensure that `AGENTS.md` remains the single source of truth for project-wide rules (tech stack, general rules, common commands, docs index).
- Ensure `git-commit-pusher.md` reflects the current git workflow conventions for committing and pushing.
- If any paths, Python versions, package names, or commands have changed, update them everywhere.
- If new features or significant patterns have been added (e.g., new status machines, new layers), document them.
- Do **not** change any source code, tests, or Makefile while doing this — only the agent markdown files.

### After Updating

Run a quick self-check by skimming the updated files side-by-side with the Makefile and a representative backend feature to confirm consistency.
