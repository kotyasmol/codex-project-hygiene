# Changelog

All notable changes are documented here. This project follows semantic versioning once it reaches a stable release; prerelease interfaces may still change when safety requires it.

## [Unreleased]

- Desktop Trash UI certification and broader adversarial coverage remain in progress.
- A restore command and durable generated-media decision ledger are under consideration.

## [0.4.0-beta] - 2026-09-01

### Changed

- Repositioned the skill around prevention before development rather than
  post-hoc deletion. The skill is now `codex-project-hygiene`, with project
  bootstrap as its primary workflow and cleanup as an explicit secondary mode.
- Reworked the persistent policy to minimize durable directories, keep
  task-scoped scratch and rejected iterations outside the repository, default
  to one generated-media candidate, and require semantic artifact names.
- Updated the UI metadata, installation instructions, and default prompt around
  project-level `AGENTS.md` bootstrap.

### Added

- `scripts/install_policy.py` previews and atomically installs one managed
  project-hygiene block in the root `AGENTS.md`.
- The preview fingerprint binds the canonical root, previous instruction file,
  and proposed content without creating a plan or state file.
- Synthetic coverage for non-mutation, existing-instruction preservation,
  stale previews, symlink destinations, malformed markers, idempotence, and
  broad-scope rejection.
- Public launch documentation, a bounded social preview, support guidance, and
  community participation standards.

### Safety changes

- Bootstrap never implies cleanup authorization and never writes to global
  Codex configuration, `AGENTS.override.md`, nested instruction files, or any
  project path other than the exact root `AGENTS.md`.
- Installation refuses symlinks, special files, invalid UTF-8, oversized files,
  duplicate or incomplete markers, and changed root/destination state.

[0.4.0-beta]: https://github.com/kotyasmol/codex-project-hygiene/releases/tag/v0.4.0-beta

## [0.3.0-beta] - 2026-08-31

### Added

- A stdout-only, metadata-only workspace hygiene audit for directory sprawl,
  persistent scratch trees, package stores, nested dependency trees, retained
  iteration families, root artifact families, and opaque generated names.
- A mergeable `AGENTS.md` fragment for semantic artifact names, minimal durable
  directory structure, and task-scoped scratch outside repositories.
- Bounded, deterministic terminal and JSON hygiene reports with no mutation
  interface and no claim that names prove Codex ownership or disposability.

### Safety changes

- Hygiene reports use a schema distinct from cleanup plans and are rejected by
  cleanup approval and apply commands.
- Hygiene traversal is descriptor-anchored, no-follow, mount-bounded, and never
  reads project file contents; special files and symlinks are skipped.
- Nested package-manager trees are grouped as logical roots instead of being
  presented as hundreds of individual cleanup candidates.
- The optional instruction fragment must be merged explicitly and never
  overwrites existing global or project guidance.

## [0.2.0-beta] - 2026-08-31

### Added

- Separate generated-media inventory and local review gallery.
- Exact SHA-256 duplicate groups and same-thread perceptual review groups.
- Explicit-ID-only media approval with Trash as the recommended mode.
- Full precondition revalidation and append-only media execution journal.
- macOS and FreeDesktop project and generated-media Trash implementations without copy-and-delete fallback.
- Public safety contract, threat model, recovery guide, security policy, and upstream storage proposal.
- macOS and Ubuntu continuous integration for the synthetic safety suite.
- Synthetic same-device Trash and manual-restore coverage for project and media workflows.
- GitHub Actions dependencies pinned to verified full commit SHAs.
- Bounded per-thread media galleries with embedded thumbnails and collection budgets.
- Linux mount-ID checks for same-filesystem bind mounts.

### Safety changes

- Generated images begin as `unknown`; no acceptance or rejection state is inferred.
- Similarity never authorizes deletion.
- Generated media cannot enter the generic directory-level cleanup workflow.
- Permanent generated-media deletion requires an additional no-recovery acknowledgment.
- Codex session JSONL and SQLite/WAL state remain structurally protected.
- Approval tokens bind the platform and exact Trash backend/root.
- Future-dated plans, protected-root nesting, forged cache object types, and
  terminal control-character spoofing fail closed.
- Descendants of process-guarded built-in caches cannot be smuggled into a
  recomputed plan, and configured generated-media roots must not be symlinks.
- Optional decoder input/output, visual grouping complexity, and gallery memory
  have explicit limits; visual previews are digest-bound to the exact file.
- Trash journals flush the expected destination before each atomic mutation.

## [0.1.0-beta]

- Initial sealed-plan workflow for reproducible dependency trees, build outputs, developer caches, and bounded Codex cleanup candidates.
