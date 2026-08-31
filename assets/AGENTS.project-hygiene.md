## Project structure and generated artifacts

Keep this repository's durable tree small, conventional, and understandable.
Before creating a file or directory, inspect the existing layout and nearby
names, then reuse the narrowest established location that fits the work.

- Create a durable directory only when the toolchain requires it or when it
  gives multiple related, user-relevant files a stable home. Do not add a
  directory merely to hold one speculative file.
- Do not create generic project-local staging trees such as `tmp`, `work`,
  `output`, `generated`, `processed`, `results`, `drafts`, `versions`, or
  `backup` unless the user requests one or an established tool requires it.
- Keep downloads, experiments, render intermediates, QA captures, extracted
  frames, and unselected generated candidates in one task-scoped temporary
  directory outside the repository. Track that exact directory and remove only
  that task-created scratch when the task is complete.
- Generate the smallest useful media batch. Default to one candidate followed
  by targeted edits; create alternatives only when the user requests them.
  Copy only selected deliverables into the repository and do not retain one
  durable file or directory per rejected iteration.
- Do not create reports, contact sheets, previews, exports, or duplicate source
  trees unless they are requested deliverables or required for verification.
- Allow standard tool-managed directories such as dependency stores and build
  outputs when the chosen toolchain requires them. Do not wrap them in custom
  containers or rename them to look cleaner.

Name durable files by meaning and follow the repository's casing convention.
Prefer `<subject>-<role>[-<semantic-variant>].<extension>`, such as
`onboarding-dashboard-mobile.png` or `billing-webhook-retry.test.ts`. Avoid
`tmp`, `new`, `copy`, `result`, `final`, `final-final`, unexplained numeric
suffixes, hashes, timestamps, and raw prompt fragments as iteration markers.

For a revision of one artifact, keep its canonical path and replace it only
when the task authorizes that change. For distinct accepted variants, use a
semantic qualifier such as `mobile`, `dark`, `print`, or `empty-state`. Never
rename, move, or delete pre-existing paths merely to enforce these preferences;
that requires a separate explicit request naming the affected scope.

Before finishing a task, review every newly created durable path. Remove only
scratch or accidental reproducible output created by the current task, and
report the durable paths that remain. Every remaining path must have a concrete
purpose tied to the user's request.
