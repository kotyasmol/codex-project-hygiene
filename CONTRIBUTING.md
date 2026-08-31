# Contributing

Contributions are welcome, especially adversarial fixtures, platform adapters, recovery improvements, and documentation that makes the risk model easier to understand.

This is a filesystem mutator. A new cleanup rule is accepted only when it proves why its target is reproducible and why important nearby content cannot match.

## Development setup

The runtime has no third-party Python dependencies. With Python 3.9 or newer:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/cleanup.py scripts/media_cleanup.py scripts/hygiene_audit.py tests/test_cleanup.py tests/test_media_cleanup.py tests/test_hygiene_audit.py tests/test_package.py
```

Run tests only against their generated temporary fixtures. Do not point development builds at real project trees or real Codex state.

## Pull-request requirements

Every behavior change should include:

- a synthetic positive fixture demonstrating the intended candidate;
- a nearby negative fixture that must remain untouched;
- a stale-plan or content-drift test when mutation is involved;
- coverage for symlinks, repository boundaries, mounts, or open handles when relevant;
- documentation of regeneration cost and platform assumptions;
- a recovery story, or a clear reason the operation must remain review-only;
- no new runtime network access or telemetry.

For a new `safe` rule, document all of the following:

1. The producer and exact artifact layout are known.
2. A strong marker proves the artifact can be reproduced.
3. Tracked content and nested repositories cannot match.
4. The candidate stays inside one approved root and filesystem.
5. Apply can revalidate the marker and filesystem identity.
6. A plausible source or deliverable with the same basename is covered by a negative test.

If any item is uncertain, classify the candidate as `review`. If the data is protected Codex state, source, a deliverable, a repository boundary, or an unsupported layout, classify it as `never-delete` or do not emit it at all.

## Generated-media rules

Perceptual similarity is never a deletion rule. New hash algorithms may improve the gallery, but must not preselect images or change an item's decision from `unknown`. Exact duplicates also require explicit IDs because identical content can still be referenced from different task histories.

Do not add code that edits session JSONL, SQLite/WAL files, or other Codex indexes to remove image references. Lifecycle-aware cleanup belongs upstream; see [the upstream proposal](docs/UPSTREAM-PROPOSAL.md).

## Workspace-hygiene rules

Hygiene detection is not a cleanup classifier. New findings must remain
metadata-only, use `origin=unknown`, set `mutation_allowed=false`, and avoid
words such as “safe,” “obsolete,” “Codex-owned,” or “duplicate” unless the claim
is independently proven outside this audit. Read no project content to infer a
finding, including README, AGENTS, manifests, source, secrets, or media.

Add bounded synthetic fixtures for hostile path controls, symlink swaps, mount
boundaries, special files, huge fanout, depth, time, and deterministic output.
Group tool-managed subtrees so the audit does not turn one package store into
hundreds of findings.

## Platform support

A platform becomes supported only after scan, tampered-plan, path-containment, Trash, permanent, interruption, and recovery tests run on that platform. Failing closed because a required primitive is unavailable is correct behavior, not a reason to weaken a check.

## Style and scope

- Keep the runtime standard-library-only.
- Prefer readable policy code over clever matching.
- Keep scan and apply separate.
- Never add `--force`, wildcard deletion, copy-and-delete Trash fallback, privilege escalation, or automatic permanent mode.
- Keep plans, fixtures, and logs free of real user content.

By contributing, you agree that your contribution is licensed under the [MIT License](LICENSE).
