# Codex Cleanup & Hygiene

**A safety-first Codex skill for diagnosing workspace sprawl, preventing future clutter, reclaiming reproducible project data, and reviewing generated images without guessing what the user meant to keep.**

Codex Cleanup & Hygiene has two deliberately separate jobs. Its hygiene audit
explains excessive directories and unclear generated names without reading file
contents or offering a delete button. Its cleanup workflows inventory dependency
trees, build outputs, developer caches, and generated media, then require an
exact approved selection and recovery mode before changing anything.

This is an independent community project and is not an official OpenAI product.

> [!IMPORTANT]
> This project is a **beta**, not a promise of zero risk. Its design is default-deny, fail-closed, and recoverability-first. Keep backups of irreplaceable work and read [the safety contract](docs/SAFETY.md) before enabling permanent deletion.

## Why this exists

Developer tools can accumulate tens of gigabytes of rebuildable dependencies, caches, worktrees, and generated media. Ordinary disk cleaners are a poor fit because a directory called `build`, an old file, or a visually similar image is not necessarily disposable.

This skill uses a narrower contract:

- scanning is read-only with respect to cleanup targets;
- broad roots such as `/`, an entire home directory, an ancestor containing that home, and all of `CODEX_HOME` are rejected;
- tracked files, repository boundaries, symlinks, mounts, deliverable trees, and protected Codex state are never deletion targets;
- every mutation is bound to a fresh plan, exact item IDs, and either Trash or permanent mode;
- filesystem identity and policy evidence are checked again immediately before mutation;
- generated-image similarity is only a review aid and never selects a file;
- workspace-hygiene findings are metadata-only observations, never deletion candidates;
- no network requests or telemetry are used at runtime.

The word `safe` in a plan means **marker-backed and reproducible**, not “delete without looking.” It still requires exact approval.

## Installation

The repository itself is the skill directory. For a personal standalone install,
clone or copy it to `$HOME/.agents/skills/codex-disk-cleanup`, then restart
Codex. If it is hosted on GitHub, you can instead ask Codex to use
`$skill-installer` with the repository URL. Review the requested source and
revision before installing a filesystem mutator.

Requirements:

- Python 3.9-compatible syntax and runtime; maintained CI targets are listed in
  [the workflow](.github/workflows/ci.yml), and an OS/Python pair is supported
  only after that matrix passes;
- no third-party Python packages;
- Git for repository-aware classification;
- `lsof` and `pgrep` for apply-time process checks on supported Unix systems;
- optionally, `ffmpeg` for faster generated-image review hashing. The standard-library PNG implementation remains available.

Then ask Codex:

```text
Use $codex-disk-cleanup to audit this project. Show me the sealed plan and estimated savings, but do not change anything until I approve exact IDs in Trash mode.
```

Or, for directory and naming hygiene only:

```text
Use $codex-disk-cleanup to audit this project's directory sprawl and generated artifact names. Do not create a report file, rename anything, or prepare a deletion plan.
```

## Workspace hygiene

Run the standalone read-only audit against one bounded project:

```bash
python3 scripts/hygiene_audit.py \
  --root /path/to/project \
  --format terminal
```

The audit is descriptor-anchored and metadata-only: it does not read README,
AGENTS, source, manifests, secrets, or media bytes. It groups tool-managed trees
such as nested `node_modules` instead of listing every internal directory. It
can highlight project-local package stores, persistent scratch, possible
iteration families, root-level artifact families, and opaque names, but always
reports origin as `unknown` and confirmed reclaimable bytes as zero.

The command writes only to stdout and has no mutation mode. Its JSON report is
not a cleanup plan and is rejected by cleanup approval/apply commands.

To influence future Codex tasks, review the optional
[`AGENTS.workspace-hygiene.md`](assets/AGENTS.workspace-hygiene.md) fragment and
the [merge instructions](references/workspace-hygiene.md). The fragment favors
existing project structure, external task-scoped scratch, the smallest useful
generated-media batch, and semantic names such as
`onboarding-dashboard-mobile.png`. It must be explicitly merged into a global
or project `AGENTS.md`; installation of this skill alone does not change ordinary
Codex file-creation behavior or the app's own media retention.

## Project cleanup

Create a new plan file for one bounded root:

```bash
python3 scripts/cleanup.py scan \
  --root /path/to/project \
  --plan /private/path/codex-cleanup-plan.json
```

Review every candidate. To prepare approval for all items classified `safe`, with recoverability:

```bash
python3 scripts/cleanup.py token \
  --plan /private/path/codex-cleanup-plan.json \
  --safe \
  --trash
```

Only after the user approves the printed plan, selection, size, mode, and token:

```bash
python3 scripts/cleanup.py apply \
  --plan /private/path/codex-cleanup-plan.json \
  --safe \
  --trash \
  --confirm APPROVAL_TOKEN
```

`review` items require explicit comma-separated IDs. `never-delete` items cannot be selected. A plan expires after 24 hours, rejects future-dated validity windows, and any relevant filesystem or policy change causes the item to be skipped. Trash approvals are also bound to the exact platform, backend, and canonical Trash root.

## Generated-image review

Codex currently does not expose a dependable machine-readable “accepted” or “rejected” flag for generated images. This skill therefore begins with every image in the `unknown` state and protects it until a person explicitly identifies the exact file.

Create a local inventory and gallery:

```bash
python3 scripts/media_cleanup.py scan \
  --plan /private/path/codex-media-plan.json \
  --gallery /private/path/codex-media-gallery.html
```

The gallery embeds bounded, lossy thumbnails rather than opening original image
bytes in the browser. Nothing is checked by default, and the displayed exact
path should be inspected with a trusted resource-limited viewer whenever fine
detail matters. Exact duplicates and same-thread visual candidates are grouped
only for review.

One gallery is capped at 200 images and 32 MiB of encoded thumbnails. For a
larger store, create bounded per-thread galleries by repeating `--thread`:

```bash
python3 scripts/media_cleanup.py scan \
  --thread THREAD_ID \
  --plan /private/path/thread-media-plan.json \
  --gallery /private/path/thread-media-gallery.html
```

After the user explicitly rejects exact IDs:

```bash
python3 scripts/media_cleanup.py token \
  --plan /private/path/codex-media-plan.json \
  --ids ID_1,ID_2 \
  --trash

python3 scripts/media_cleanup.py apply \
  --plan /private/path/codex-media-plan.json \
  --ids ID_1,ID_2 \
  --trash \
  --confirm APPROVAL_TOKEN
```

The skill never edits Codex session JSONL or SQLite state. See [Generated media](docs/GENERATED-MEDIA.md) for the evidence, limitations, and the proposed upstream fix.

## Support matrix

This is deliberately narrower than “runs everywhere.” A filesystem mutator should earn platform support with tests.

| Platform | Hygiene audit | Cleanup audit and plan | Generated-media review | Trash implementation | Status |
|---|---:|---:|---:|---:|---|
| macOS | Yes | Yes | Yes | Implemented, not recovery-certified | Experimental beta |
| Linux | Yes | Yes | Yes | FreeDesktop implementation, not recovery-certified | Experimental beta |
| Windows | Hard-disabled | Not claimed / untested | Not claimed / untested | No; both apply commands are hard-disabled | No support claim |

The checked-in CI workflow runs the synthetic scan, policy, tamper,
permanent-mode, same-device Trash, manual-restore, mount-ID, decoder-budget, and
media-consent safety suite on macOS and Ubuntu. A release must not claim that
matrix passed until the public workflow is green. It does not certify a desktop
environment's **Put Back** UI. Network filesystems, unusual containers, and
privilege escalation are outside the claim.

## What it will not do

- infer rejection from age, filename, prompt similarity, visual similarity, or “all but newest”;
- infer that a path was created by Codex or is obsolete from its name;
- automatically rename or reorganize pre-existing project content;
- clean `/`, a whole home directory, or all Codex state;
- follow symlinks, cross mounts, delete repositories, or use copy-then-delete as a Trash fallback;
- edit `sessions`, archived session history, rollout JSONL, SQLite/WAL files, configuration, skills, or plugins;
- use `rm -rf`, `git clean`, shell globs, hidden force flags, network calls, or telemetry;
- promise that Trash immediately reclaims space—it does not until Trash is emptied;
- prevent Codex from retaining future images at the application level. That requires an upstream lifecycle change.

## Documentation

- [Safety contract](docs/SAFETY.md)
- [Threat model](docs/THREAT-MODEL.md)
- [Generated-media policy and audit evidence](docs/GENERATED-MEDIA.md)
- [Workspace-hygiene policy](references/workspace-hygiene.md)
- [Recovery guide](docs/RECOVERY.md)
- [Upstream storage proposal](docs/UPSTREAM-PROPOSAL.md)
- [Maintainer release checklist](docs/RELEASE-CHECKLIST.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## License

[MIT](LICENSE)
