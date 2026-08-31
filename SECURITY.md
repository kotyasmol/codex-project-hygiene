# Security policy

Codex Disk Cleanup mutates files, so a false-positive deletion path is a security issue even when no code execution is involved. Please report path-containment bypasses, plan-validation bypasses, symlink or mount escapes, time-of-check/time-of-use flaws, unintended Codex-state deletion, approval-token confusion, and recovery failures as security-sensitive.

## Reporting a vulnerability

Use GitHub's **Report a vulnerability** form for this repository when private vulnerability reporting is enabled. If it is unavailable, open a minimal public issue asking the maintainers to enable a private channel. Do not include exploit details in that issue.

Please include, when safe to do so:

- the affected release or commit;
- operating system, filesystem type, and Python version;
- whether the issue affects scan, token, Trash apply, or permanent apply;
- a reproduction using synthetic files in a temporary directory;
- the expected fail-closed behavior and what happened instead.

Do **not** upload real plans, galleries, generated images, session JSONL, SQLite files, prompts, secrets, usernames, absolute home paths, or execution journals. Replace them with a minimal synthetic fixture.

Maintainers should acknowledge a complete report as soon as practical, keep the reporter informed while the issue is reproduced, and coordinate disclosure after a fix and regression test are available. As a volunteer beta project, this policy does not promise a response SLA.

## Supported versions

Security fixes target the current default branch and the newest published beta. Older snapshots may not receive backports. Until a stable release exists, users should update before performing a new cleanup.

## Safe research boundary

Use only data and machines you are authorized to test. Prefer disposable temporary directories. Do not test by placing important data behind a suspected bypass, and do not publish a working destructive exploit before a fix is available.
