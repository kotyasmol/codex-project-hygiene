# Persistent project-hygiene instructions

Use the bundled installer when the user wants Codex to prevent new workspace
clutter before development starts. The resulting managed block affects future
file creation; it does not authorize cleanup, renaming, or reorganization of
pre-existing content.

## Why the policy goes in AGENTS.md

A skill is loaded only when a task selects or invokes it. Installation alone
does not inject the skill into every later task. A root-level `AGENTS.md` is the
durable project instruction surface that subsequent Codex tasks discover.

This project intentionally installs only at project scope. It does not write to
the Codex home directory, global configuration, `AGENTS.override.md`, or nested
instruction files. Users who want a global preference should make that separate
scope choice themselves after reviewing the project policy.

## Safe installation

Run `scripts/install_policy.py preview --root <project>` first. Preview prints
the exact target, a unified diff, and an approval token without changing the
project. The token binds the canonical project root, previous `AGENTS.md`
content, and proposed content.

`scripts/install_policy.py apply --root <project> --confirm <token>` then:

- re-resolves the same bounded project root;
- refuses a symlinked root or `AGENTS.md`, special files, invalid UTF-8, files
  larger than 256 KiB, and malformed or duplicate managed markers;
- preserves every byte outside its marked block;
- rejects content or root identity changes after preview;
- writes the result through a same-directory temporary file and atomic replace;
- removes its temporary file on failure.

No backup or state file is created. Git or the user's ordinary backup system
remains the recovery mechanism for project instructions.

## Instruction precedence and conflicts

Inspect all applicable instruction files before installation. Preserve explicit
user choices, repository-specific tool requirements, and more local guidance.
If the bundled block contradicts an existing rule, explain the exact conflict
instead of silently installing it. Do not modify an override file unless the
user explicitly asks for a separate manual change.

If the managed block is already current, apply is a no-op. If its markers are
missing, duplicated, or reordered, stop for manual review rather than guessing
which content belongs to the skill.

## Manual removal

Removal is intentionally not automated in this beta. After an explicit request,
remove the single block from
`<!-- codex-project-hygiene:start -->` through
`<!-- codex-project-hygiene:end -->` with a normal reviewed edit. Preserve all
surrounding content and do not delete `AGENTS.md` when unrelated instructions
remain.
