# Support

Codex Project Hygiene is an independent community project, not an official
OpenAI product. Maintainers provide best-effort support without an SLA.

## Before opening an issue

1. Use the newest published beta.
2. Run only a preview or read-only audit while diagnosing the problem.
3. Reproduce the behavior with synthetic files in a temporary directory.
4. Remove usernames, absolute home paths, prompts, images, plans, journals,
   sessions, databases, tokens, and secrets.

Use the repository's issue forms for ordinary bugs, false positives, and feature
proposals. Include the operating system, filesystem type, Python version,
release or commit, exact sanitized command, expected result, and actual result.

Questions about installing or bootstrapping the skill can use GitHub
Discussions. Search existing issues and discussions first.

## Security and deletion-safety reports

Do not open a public issue for a containment bypass, unintended deletion,
symlink or mount escape, approval bypass, or exposed private data. Follow the
private process in [SECURITY.md](SECURITY.md).

Never attach real project content or Codex state. A minimal synthetic fixture is
both safer and easier to turn into a regression test.
