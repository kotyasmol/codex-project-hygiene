# Safety contract

Codex Project Hygiene is designed to prevent workspace sprawl and make unsafe
cleanup difficult to express. It is not “100% safe,” and it cannot replace
backups, filesystem snapshots, or human judgment about irreplaceable work.

The contract below is normative. A change that weakens it is a breaking safety change even if the command-line interface stays the same.

## 0. Bootstrap is narrow and separate from cleanup

Project bootstrap changes only the exact project-root `AGENTS.md` after showing
a diff and operation fingerprint. It preserves content outside one managed
block, refuses symlinks and malformed markers, revalidates root and destination
state, and writes atomically. It creates no policy state, backup, report,
background process, global instruction, override, or nested instruction file.

Installing or updating the preventive policy never authorizes scanning or
deleting existing data. Conversely, an audit request never authorizes installing
persistent instructions. Existing project paths are not renamed, moved, or
deleted to enforce the policy.

## 1. Scan does not mutate cleanup targets

`scan` reads candidate roots and writes only new user-designated artifacts such as a plan or local gallery. It refuses to overwrite an existing output or follow an output symlink. It does not rename, truncate, delete, quarantine, or move a cleanup target. Terminal output JSON-quotes untrusted paths so control characters in filenames cannot spoof the visible approval list.

The output file itself is a side effect. Put plans and galleries in a private temporary directory because they can contain absolute paths and bounded local image thumbnails.

The workspace-hygiene audit is stricter: it writes only to stdout, creates no
plan or gallery, reads project metadata but not project file contents, and has
no mutation command. Its report has a distinct schema, sets
`mutation_allowed=false`, and cannot be used by cleanup approval or apply.

## 2. Scope is bounded

Every project root must be explicit. The helper rejects broad or protected roots, including `/`, an entire home directory, an ancestor that contains that home, protected output/metadata boundaries, and the whole Codex state directory. It does not discover arbitrary disks, cross mount points, or follow symlinks. On Linux, mount IDs from `/proc/self/mountinfo` supplement device checks so same-filesystem bind mounts also fail closed.

`output/`, `outputs/`, and `assets/` are protected deliverable trees. A dated Codex folder, `work/`, `tmp/`, `build/`, `dist/`, `backup/`, or `archive/` is not disposable merely because of its name.

## 3. Classification is conservative

Plan entries have one of three policy outcomes:

- `safe`: reproducible, marker-backed data whose rule passed repository and policy checks;
- `review`: potentially reproducible or generated data that needs exact human selection;
- `never-delete`: protected state or a boundary the helper cannot select.

`safe` does not mean “automatic.” Every mutation still needs a concrete approval operation. Age, size, basename, visual similarity, and prompt similarity are never sufficient evidence.

## 4. Approval binds one operation

The token command derives an operation fingerprint from the sealed plan, sorted exact item IDs, mutation mode, platform, and the exact Trash backend/root when applicable. Changing any of those inputs changes the token. Review items cannot be selected by a wildcard; generated media always requires explicit IDs.

The token is an integrity and operation-binding mechanism. It is **not** a digital signature, a secret, authentication, or proof that a particular human gave consent. The surrounding Codex workflow must show the plan, IDs, estimated size, and mode to the user and wait for their explicit approval.

## 5. Plans are short-lived and revalidated

Plans expire after 24 hours, reject validity windows created too far in the future, and are tied to the host and Codex path context. Before each mutation, the helper checks applicable invariants again, including:

- canonical containment within the approved root;
- path type and symlink status;
- device, inode, mode, size, and metadata fingerprint;
- relevant marker, lockfile, and Git evidence;
- repository and worktree boundaries;
- required processes and open file handles;
- full SHA-256 content for generated media;
- exact plan and selection consistency.

If an invariant cannot be verified, the item is skipped. Uncertainty fails closed.

## 6. Atomic local moves precede destruction

Trash mode is the default recommendation. Moves use a same-filesystem atomic rename and refuse a copy-and-delete fallback. This avoids deleting the source after a partial copy. The relocated object is checked again, and the helper attempts to restore it if the check fails.

Permanent deletion first renames the target to a unique adjacent tombstone, revalidates it, and only then removes it. Both permanent workflows additionally require `--ack-no-recovery`. Permanent mode has no recovery promise and should be reserved for explicit, audited use.

Moving to Trash does not reclaim storage until the user empties Trash separately.

## 7. Codex state is protected

The helper does not manually delete or edit:

- session or archived-session history;
- rollout JSONL;
- SQLite databases, WAL files, or indexes;
- task locks and IPC state;
- configuration, credentials, installed skills, or plugins;
- whole Codex Application Support or `CODEX_HOME` trees;
- Codex-managed repository/worktree roots.

Managed worktrees should be retired through Codex's own task lifecycle. Generated media has a separate explicit-file review workflow; it never modifies history or database references.

## 8. Local-only runtime

The helpers use no network calls, analytics, update checks, or telemetry. Plans, galleries, hashes, paths, and journals remain local unless the user chooses to share them. Optional `ffmpeg` is invoked only as a local executable to accelerate a review-only visual hash.

## 9. Execution is auditable

Apply reports changed, skipped, and failed items and writes a private result or journal beside the plan. Generated-media journaling is opened before the first mutation and flushed after each event. Logs contain paths and selection metadata, so they must be treated as sensitive local files.

An error or skip does not justify falling back to `rm`, a wildcard, `git clean`, a broader root, another filesystem, or privilege escalation. Fix the cause, rescan, and obtain a new approval.

## 10. Hygiene findings never authorize cleanup

Directory and file names are untrusted metadata. A path named `tmp`, `final`,
`output`, `.codex-generated`, or `v2` does not prove who created it, whether it
is accepted, or whether it is reproducible. Hygiene findings therefore use
`origin=unknown`, never classify an item as safe, and report zero confirmed
reclaimable bytes.

Traversal is bounded and anchored to open directory descriptors with no-follow
semantics. Symlinks, special files, mount boundaries, protected metadata trees,
permission failures, races, and exhausted budgets are skipped or make coverage
partial. Partial coverage must never be presented as a clean verdict.

The optional workspace-instruction fragment affects future file creation only
after an explicit merge into an applicable `AGENTS.md`. It never authorizes
automatic cleanup, renaming, or reorganization of existing paths.

## Known limitations

- A local administrator, compromised Python interpreter, malicious kernel, or hostile filesystem can defeat user-space checks.
- Allocated-size estimates can differ from space actually reclaimed because of clones, hard links, compression, snapshots, and filesystem accounting.
- Open-handle and process checks depend on platform tools and may fail closed when unavailable.
- Trash semantics differ across operating systems and desktop environments.
- A plan can reveal sensitive path names; a gallery can reveal generated content.
- The skill cannot infer semantic value or whether an image was emotionally or commercially important.
- Removing a generated image referenced by history may leave that historical preview unavailable; exact user rejection is therefore required.
- Windows mutation support is not currently claimed.
- Windows workspace-hygiene auditing is hard-disabled in this release.

For abuse cases and residual risk, see [Threat model](THREAT-MODEL.md). For restoration steps, see [Recovery](RECOVERY.md).
