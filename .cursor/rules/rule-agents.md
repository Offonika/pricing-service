# .cursor/rules/rule-agents.md

The pricing-service project has a dedicated specification for AI agents:

- docs/agents.md — main documentation for agents:
  - responsibilities and roles of each agent,
  - architecture and data flow,
  - conventions for adding and modifying agents,
  - integration points with app/, services/, tasks/.

Rules for the Agent:

1. Before changing any code in:
   - /agents
   - app/* related to agents (routers, dependencies, startup code)
   - services/* that are called by agents

   → first open and read docs/agents.md to understand the current design.

2. All changes to agents must be consistent with docs/agents.md and PRD.md.

3. If the Agent introduces new behavior for agents, new commands, or new workflows:
   - update docs/agents.md accordingly;
   - if the change is significant, also update PRD.md and docs/architecture.md.

4. Do not introduce alternative agent architectures that conflict with docs/agents.md.
   Extend and refine the existing design instead.

5. When in doubt about agents behavior:
   - treat docs/agents.md as the primary reference;
   - PRD.md is the higher-level product description, but docs/agents.md governs implementation details.
