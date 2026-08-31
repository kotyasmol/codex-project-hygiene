## Artifact naming and workspace hygiene

Treat user-selected paths and established repository conventions as
authoritative. Inspect nearby names and relevant configuration before creating
files. Never automatically rename, move, or delete a pre-existing project path;
that requires a separate explicit request naming the affected scope.

- Keep the durable project tree minimal. Create only requested deliverables and
  files needed to run, test, or maintain them; reuse existing source, asset,
  documentation, and test locations.
- Do not create a directory for speculative use or merely to hold one file
  unless the repository or tool convention calls for it.
- Do not introduce generic staging directories such as `tmp`, `work`,
  `output`, `generated`, `processed`, `results`, `drafts`, or `versions` unless
  the user requests one or the repository or tool already requires it.
- Put downloads, experiments, render intermediates, QA captures, extracted
  frames, and generated candidates in a private task-scoped temporary directory
  outside the project. Copy only accepted deliverables into the project.
- For generated media, request the smallest useful candidate batch and prefer
  one candidate followed by targeted edits unless the user asks for alternatives.
  Do not create one durable file or directory per rejected iteration.
- Remove only scratch created by the current task when removal is safe. Never
  infer that a pre-existing path is disposable from its name.

Name durable artifacts by meaning, using the repository's casing convention.
Prefer `<subject>-<role>[-<semantic-variant>].<extension>`, for example
`studyingcrm-logo-dark.svg` or `onboarding-dashboard-mobile.png`. Avoid `tmp`,
`new`, `copy`, `result`, `final`, `final-final`, unexplained numeric versions,
hashes, timestamps, and raw prompt fragments as iteration markers.

For a revision of the same artifact, keep its canonical path and replace it
only when authorized. For genuinely distinct accepted variants, add a semantic
qualifier such as `mobile`, `dark`, `mark`, or `print`. Use an established
release version or date only when it carries real domain meaning; do not create
`v2`, `v3`, or `final-2` merely because a preferred name already exists.

Allow standard tool-managed directories when the toolchain requires them. Do
not rename them or wrap them in custom folders. Before finishing, review newly
created paths and remove only task-created scratch or accidental reproducible
output. Every durable new path should have a clear purpose tied to the request.
