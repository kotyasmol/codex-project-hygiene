# Workspace-hygiene instructions

Use the mergeable fragment in
`assets/AGENTS.workspace-hygiene.md` when the user wants Codex to prevent new
workspace clutter, not merely audit existing files. The fragment sets defaults
for paths created by future tasks; it does not authorize cleanup, renaming, or
reorganization of existing content.

## Choose the instruction scope

- **Global:** add the fragment to `AGENTS.md` in the active Codex home (normally
  `~/.codex/AGENTS.md`) only when the user wants the preference in every
  repository. Resolve the actual `CODEX_HOME` first. A global
  `AGENTS.override.md`, when present, takes precedence over the ordinary global
  file.
- **Project:** add the fragment to the repository-root `AGENTS.md` when the
  preference should apply only to that repository. More specific instructions
  closer to a working directory can override broader project guidance.

Do not choose global scope merely because the request sounds general. Confirm
the intended scope when it is not already explicit. Do not install this
fragment during an audit-only request.

## Merge without damage

1. Inspect the destination and every applicable existing instruction file.
2. If the destination exists, merge the fragment as one clearly headed section;
   preserve its content, ordering constraints, and repository-specific rules.
   Resolve contradictions in favor of explicit user choices and the more local
   convention.
3. If the destination does not exist, show the exact path and proposed scope
   before creating it. Never overwrite an existing `AGENTS.md` or
   `AGENTS.override.md`, and never replace a whole file with this template.
4. Do not add duplicate copies when equivalent guidance is already active. Do
   not modify an override file unless the user explicitly chose that file.
5. After an approved change, reread the resulting file and report the exact path
   and scope. Starting a new Codex run may be necessary for newly installed
   instructions to be discovered.

Official OpenAI documentation for discovery order, global guidance, project
instructions, and overrides:

https://learn.chatgpt.com/docs/agent-configuration/agents-md
