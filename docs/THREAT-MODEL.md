# Threat model

## Security objective

Reclaim storage from reproducible or explicitly rejected local artifacts without deleting source, deliverables, protected Codex state, repositories, active files, or anything outside the exact approved operation.

Availability and integrity of user data take priority over maximizing reclaimed bytes. A false negative leaves junk behind; a false positive can destroy work. The design intentionally prefers false negatives.

## Assets

- source code, uncommitted changes, repositories, and worktrees;
- project deliverables, exports, assets, archives, and backups;
- environments and build outputs that are expensive or impossible to reproduce;
- generated images and the user's acceptance/rejection intent;
- Codex task history, JSONL, databases, locks, configuration, credentials, skills, and plugins;
- private paths, prompts, and image content exposed by plans or galleries;
- the user's ability to recover an item after a mistake.

## Trust boundaries

The following inputs are untrusted and must be validated independently:

- command-line roots, output paths, IDs, modes, and approval tokens;
- plan JSON, even when it has a matching unkeyed checksum;
- directory names and file extensions;
- path names that contain terminal controls, bidirectional text, or instruction-like prose;
- filesystem state after scan;
- Git metadata and ignore results;
- process-list and open-handle probes;
- optional `ffmpeg` output;
- visual and exact-duplicate group membership;
- Codex history references, which are not acceptance signals.

The operating system, Python runtime, and current user account are trusted to enforce ordinary local filesystem semantics. Root compromise and a malicious kernel are out of scope.

## Abuse and failure cases

| Case | Risk | Required control |
|---|---|---|
| Broad root or path traversal | Deletes unrelated data | Reject broad/protected roots; canonical containment per item |
| Symlink swap | Redirects mutation outside the plan | Never select symlinks; use identity checks and atomic rename |
| Mount crossing | Reads or mutates data outside the approved tree | Device checks everywhere; Linux mount-ID checks catch same-device bind mounts; require same-device Trash rename |
| Repository nested in cache-looking path | Deletes source/worktree | Detect VCS boundaries and tracked content; classify as protected |
| Malicious or hand-edited plan | Injects paths or changes policy | Strict schema, host/context, path, ID, selection, and seal validation |
| Stale plan / TOCTOU | Target changes between review and apply | Short TTL and immediate precondition revalidation per item |
| Running process writes target | Corruption or partial recreation | Open-handle/process probes; fail closed when unverifiable |
| Approval replay, destination, or mode confusion | Applies a different selection or Trash destination | Bind token to plan seal, exact sorted IDs, mode, platform, backend, and canonical Trash root |
| Similar-looking images | Deletes a distinct wanted image | Similarity is review-only and cannot preselect |
| Exact duplicate is referenced twice | Breaks one history path | Exact duplicate status is still review-only; require every exact ID |
| Corrupt image, decompression bomb, or decoder flood | Excess memory/CPU/disk during scan | Input, pixel, decoded-byte, worker, gallery, and decoder stdout/stderr limits; warning or fail closed |
| Thumbnail/content race | User rejects one preview but another exact file is selected | Bind visual-source digest, exact SHA-256, and filesystem identity across inspection |
| Huge gallery or similarity group | Quadratic CPU or multi-gigabyte HTML/RAM | Per-gallery item/byte budgets, bounded thumbnails, per-thread filters, and a grouping complexity cap |
| Interrupted mutation | Lost audit trail or stranded tombstone | Atomic rename, pre-mutation fsynced attempt with expected destination, recovery-state reporting, recovery guide |
| Copy-to-Trash failure | Source removed before durable destination | Forbid copy-and-delete; require same-filesystem atomic rename |
| Plan/gallery disclosure | Leaks usernames, paths, or image content | New private files, no network, local-only gallery, sharing warnings |
| “Clean harder” fallback | Bypasses safety after a skip | No force mode; rescan instead of `rm`, globs, or privilege escalation |
| Hygiene report reused as cleanup plan | Converts a visual observation into deletion authority | Distinct report kind/schema; mutation flags are false; token/apply reject it |
| Instruction injection in a path | Tricks the operator or agent into following filename text | Never read project contents for hygiene; JSON-quote terminal paths; treat all names only as metadata |
| Symlink or directory swap during hygiene traversal | Reads outside the approved project | Descriptor-anchored no-follow opens, identity checks, root-component validation, and partial coverage on races |
| FIFO, device, or socket disguised as a manifest | Blocks or streams data during an audit | Never parse manifests; inspect type through anchored metadata and skip non-regular files |
| Huge or deeply nested tree | Exhausts memory, CPU, or report size | Global entry/path/time/finding budgets, pre-materialization child cap, and explicit partial status |
| Name such as `tmp`, `final`, or `.codex-generated` | Falsely assigns Codex ownership or obsolescence | Provenance remains `unknown`; names never authorize mutation or confirmed savings |
| Nested package-manager internals | Produces another unusable wall of paths | Group the outer logical dependency/store tree and sample bounded paths |

## Generated-media decision model

The only safe local states are:

- `unknown`: protected; the default for every discovered image;
- `rejected`: assigned only to exact IDs selected by the user for this operation;
- `kept`: conceptually protected, whether explicitly kept or merely uncertain.

There is no reliable accepted/rejected field in current Codex state. A saved path, history reference, image age, later variation, closed gallery, or similarity group does not prove rejection. When signals conflict, keeping wins.

## Out of scope

- defending against root, kernel, Python-runtime, or account compromise;
- secure erasure from SSDs, snapshots, backups, cloud sync, or filesystem journals;
- recovering permanent deletions;
- determining copyright, privacy, or business value of generated media;
- proving user identity from an approval token;
- changing Codex's own retention and deduplication behavior;
- guaranteeing exact reclaimed bytes across filesystems.

## Security regression rule

Every accepted mutation path needs a synthetic positive fixture and at least one nearby negative fixture. A bug that expands scope, bypasses exact approval, converts uncertainty into deletion, or prevents recovery is treated as security-sensitive. See [SECURITY.md](../SECURITY.md).
