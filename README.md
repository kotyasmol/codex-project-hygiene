# Codex Project Hygiene

**A preventive Codex skill that installs persistent project instructions before
development starts, so future tasks create fewer directories, keep temporary
iterations outside the repository, and give durable artifacts meaningful names.**

Most cleanup tools arrive after a workspace has already become a landfill. This
skill starts earlier: it adds one reviewed block to the project's root
`AGENTS.md`. Codex then discovers the rules in later tasks without requiring the
skill to be invoked for every change.

Cleanup helpers for existing projects remain available as an explicit secondary
mode. They are not activated by project bootstrap.

This is an independent community project and is not an official OpenAI product.

## What changes after bootstrap

Future Codex tasks are instructed to:

- inspect and reuse the established project layout before creating paths;
- create a directory only for a real toolchain requirement or a coherent group
  of durable deliverables;
- keep downloads, experiments, renders, captures, and rejected candidates in
  one task-scoped temporary directory outside the repository;
- generate one media candidate by default and iterate on it unless alternatives
  were requested;
- copy only selected deliverables into the project;
- use semantic names such as `onboarding-dashboard-mobile.png` instead of
  `final-2.png`, `result-new.png`, timestamps, hashes, or raw prompt fragments;
- report the durable paths created and remove only scratch from the current task.

The policy respects user-requested paths, repository conventions, and standard
tool-managed directories. It never authorizes reorganizing or deleting existing
content.

## Install the skill

The repository itself is the skill directory. Clone or copy it to:

```text
$HOME/.agents/skills/codex-project-hygiene
```

Then restart Codex. You can also ask Codex to use `$skill-installer` with:

```text
https://github.com/kotyasmol/codex-disk-cleanup
```

The repository currently retains its original cleanup-era URL; the installed
skill name is `codex-project-hygiene`.

Review the source and revision before installing any skill that can modify
project instructions or files.

Runtime requirements are Python 3.9 or newer and Git. There are no third-party
Python dependencies and no runtime network calls or telemetry.

## Bootstrap a new project

At the beginning of development, ask:

```text
Use $codex-project-hygiene to bootstrap this project. Preview the exact
project-level AGENTS.md change, preserve existing instructions, and install it
after I approve the target, diff, and fingerprint. Do not create anything else.
```

The skill first runs a non-mutating preview:

```bash
python3 scripts/install_policy.py preview \
  --root /path/to/project \
  --format terminal
```

Preview shows the exact `AGENTS.md` target, action, unified diff, and approval
token. After approval:

```bash
python3 scripts/install_policy.py apply \
  --root /path/to/project \
  --confirm APPROVAL_TOKEN
```

The installer changes only the root `AGENTS.md`. It preserves unrelated bytes,
owns only one explicitly marked block, performs an atomic replacement, and
rejects symlinks, broad roots, malformed markers, invalid UTF-8, oversized
instruction files, or a stale fingerprint. It creates no config directory,
backup, policy database, report, or daemon.

Installing the skill alone is not enough: skills are selected per task. The
bootstrap step is what makes the guidance persistent for that project.

## The installed policy

The maintained source is
[`assets/AGENTS.project-hygiene.md`](assets/AGENTS.project-hygiene.md). The
installer surrounds it with:

```html
<!-- codex-project-hygiene:start -->
...
<!-- codex-project-hygiene:end -->
```

Running bootstrap again updates that block without duplicating it. Conflicting,
missing, or duplicate markers fail closed for manual review. See
[instruction precedence and removal](references/workspace-hygiene.md).

## Audit an existing messy project

The read-only audit examines names and directory metadata without reading file
contents or writing a report file:

```bash
python3 scripts/hygiene_audit.py \
  --root /path/to/project \
  --format terminal
```

It can highlight persistent scratch trees, project-local package stores,
iteration families, opaque names, and excessive root artifacts. Every finding
has unknown origin and zero confirmed reclaimable bytes. A filename does not
prove that Codex created it or that it is safe to remove.

## Optional cleanup for existing data

Only an explicit cleanup request activates these tools:

- `scripts/cleanup.py` inventories bounded dependency trees, build outputs, and
  developer caches;
- `scripts/media_cleanup.py` creates a bounded local review gallery for exact
  generated-image selection.

Cleanup requires a sealed plan, visible exact IDs, an operation-bound token,
and either Trash or permanent mode. Trash is recommended; permanent deletion
requires a separate no-recovery acknowledgement. Tracked files, repositories,
symlinks, mount boundaries, deliverable trees, and protected Codex state cannot
be selected.

Visual similarity and exact duplication only create review groups. They never
select an image. Codex currently exposes no dependable machine-readable
accepted/rejected flag, so the skill cannot safely delete rejected images in the
background or prevent the application from retaining history.

## Safety boundary

- Project bootstrap never implies cleanup permission.
- The policy installer writes only the exact project-level `AGENTS.md`.
- Existing files are never renamed, moved, or deleted to enforce neatness.
- Cleanup scans reject `/`, a home directory, broad ancestors, and all of
  `CODEX_HOME`.
- Cleanup never edits Codex sessions, JSONL history, SQLite/WAL state, task
  locks, configuration, installed skills, or plugins.
- Cleanup uses no `rm -rf`, `git clean`, wildcard selection, force flag,
  privilege escalation, or copy-and-delete Trash fallback.

Read the [safety contract](docs/SAFETY.md),
[threat model](docs/THREAT-MODEL.md), and
[recovery guide](docs/RECOVERY.md) before enabling cleanup. Cleanup support is a
safety-first beta, not a promise of zero risk.

## Platform status

The policy installer uses standard-library Python and is tested with synthetic
fixtures. The metadata audit and cleanup tools remain deliberately narrower:

| Platform | Policy bootstrap | Hygiene audit | Cleanup apply |
|---|---:|---:|---:|
| macOS | Yes | Yes | Experimental beta |
| Linux | Yes | Yes | Experimental beta |
| Windows | Implemented, not yet CI-certified | Hard-disabled in this beta | Hard-disabled |

No platform support claim is final until the public CI matrix is green.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py tests/*.py
```

Contributions should use only synthetic fixtures and must preserve the
default-deny boundaries. See [CONTRIBUTING.md](CONTRIBUTING.md) and
[SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
