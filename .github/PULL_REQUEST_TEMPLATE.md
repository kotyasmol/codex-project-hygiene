## What changed

Describe the narrow behavior change and why it belongs in a prevention-first
project-hygiene skill.

## Safety argument

- For policy changes: what future clutter is prevented, and which established
  project conventions or explicit user choices still take precedence?
- For cleanup changes: what exact artifact becomes eligible, what proves it is
  reproducible, how is the target revalidated, and what is the recovery path?

## Tests

List synthetic positive, negative, stale-plan, and platform tests. Do not attach plans or fixtures containing real user paths or content.

## Checklist

- [ ] Scanning remains non-mutating with respect to cleanup targets.
- [ ] Bootstrap still writes only its managed root `AGENTS.md` block.
- [ ] Policy guidance does not silently reorganize or delete existing content.
- [ ] The change does not broaden roots, follow symlinks, cross mounts, or enter repository/protected Codex boundaries.
- [ ] Uncertainty remains `review`, `never-delete`, or skipped.
- [ ] Generated-media similarity does not select files.
- [ ] Exact approval and fail-closed revalidation are preserved.
- [ ] Trash remains the recommended mode; no copy-and-delete fallback or force flag was added.
- [ ] No runtime network request, telemetry, or third-party Python dependency was added.
- [ ] Documentation and recovery guidance were updated.
- [ ] `python3 -m unittest discover -s tests -v` passes.
