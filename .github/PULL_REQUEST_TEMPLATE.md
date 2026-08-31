## What changed

Describe the narrow behavior change and why it belongs in a safety-first cleanup skill.

## Safety argument

- What exact artifact becomes eligible?
- What marker proves it is reproducible or explicitly rejected?
- Which nearby source/deliverable layout could look similar?
- How does apply revalidate the target?
- What is the recovery path?

## Tests

List synthetic positive, negative, stale-plan, and platform tests. Do not attach plans or fixtures containing real user paths or content.

## Checklist

- [ ] Scanning remains non-mutating with respect to cleanup targets.
- [ ] The change does not broaden roots, follow symlinks, cross mounts, or enter repository/protected Codex boundaries.
- [ ] Uncertainty remains `review`, `never-delete`, or skipped.
- [ ] Generated-media similarity does not select files.
- [ ] Exact approval and fail-closed revalidation are preserved.
- [ ] Trash remains the recommended mode; no copy-and-delete fallback or force flag was added.
- [ ] No runtime network request, telemetry, or third-party Python dependency was added.
- [ ] Documentation and recovery guidance were updated.
- [ ] `python3 -m unittest discover -s tests -v` passes.
