# Maintainer release checklist

Use this checklist before publishing a beta or stable release.

## Repository setup

- Enable private vulnerability reporting and point contributors to
  [`SECURITY.md`](../SECURITY.md).
- Require pull-request review and passing CI for the default branch.
- Enable Dependabot or Renovate for pinned GitHub Actions commit updates; keep
  the human-readable release tag in an inline comment.
- Suggested repository topics: `codex`, `openai-codex`, `agents-md`,
  `project-hygiene`, `developer-tools`, `python`, `safety`, and `ai-tools`.
- State clearly that this is an independent community project, not an official
  OpenAI product.

## Release evidence

- Run the full test matrix on macOS and Linux using every advertised Python
  version.
- Run `quick_validate.py` from the official Codex skill-creator tooling.
- Verify policy preview is non-mutating, installation preserves unrelated
  instructions, stale fingerprints and symlinks fail closed, and a second
  installation creates no duplicate managed block.
- Verify scan, recomputed-plan rejection, selection/mode binding, Trash,
  manual restore, and permanent acknowledgement on synthetic fixtures only.
- Verify that the hygiene audit is stdout-only, metadata-only, deterministic,
  bounded, symlink/mount safe, nonblocking on special files, and rejected as a
  cleanup plan. Run an instruction-like hostile-filename fixture and confirm no
  project entry changes.
- Review the `AGENTS.md` fragment for semantic naming, external task scratch,
  minimal media batches, useful directory boundaries, and an explicit
  prohibition on renaming or deleting pre-existing paths.
- Review all platform claims against the exact tests that ran. Do not promote a
  platform from audit-only based on code inspection alone.
- Confirm that the release archive contains no plan, gallery, journal, real
  path, generated image, session, database, cache, or bytecode artifact.
- Publish a SHA-256 checksum for the release archive and sign the release when a
  maintainer signing identity is available.

## Communication

- Lead release notes with safety changes and known limitations.
- Never advertise the project as “100% safe” or promise exact reclaimed bytes.
- Ask false-positive reporters for a synthetic fixture, never their real plan,
  gallery, task history, or generated images.
