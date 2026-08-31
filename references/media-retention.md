# Generated-media retention

Use this workflow only for Codex-generated images under the active
`$CODEX_HOME/generated_images` root. The helper is local-only and performs no
network requests or telemetry.

## What the evidence can and cannot prove

Codex history currently does not expose a reliable machine-readable
accepted/rejected/selected state for generated images. A completed generation
event proves that an image was produced, not whether the user liked it.

Treat every image as unknown until the user explicitly names its plan ID for
rejection. Exact byte equality does not prove rejection. A visual-hash cluster
is review assistance only and may contain false positives; it never selects or
authorizes a deletion.

Do not edit or compact session JSONL, archived-session JSONL, SQLite databases,
WAL files, or history references to reclaim media space. Those stores may
contain referenced image paths or embedded data and can become inconsistent if
rewritten independently.

## Local review workflow

Resolve `<skill-dir>` as the directory containing the parent `SKILL.md`. Create
both outputs at paths that do not already exist:

```bash
python3 <skill-dir>/scripts/media_cleanup.py scan \
  --plan <new-secure-path>/media-plan.json \
  --gallery <new-secure-path>/media-review.html
```

The root defaults to the active `$CODEX_HOME/generated_images`. If a custom
Codex home is in use, pass its exact generated-images directory with
`--root <exact-generated_images-root>`. Do not point the helper at an arbitrary
photo library or a broader Codex directory.

The static gallery remains on the local machine. It starts with no deletion
selection. It embeds bounded, lossy thumbnails rather than loading originals
through `file:` URLs. Similar-looking images are grouped only to make human review faster.
Ask the user to inspect the gallery and provide the exact IDs they reject. Do
not translate silence, task age, thread archival, or similarity into rejection.
When text or fine detail matters, inspect the displayed exact path with a
trusted resource-limited viewer before selecting it.

One gallery is limited to 200 images and 32 MiB of encoded thumbnails. For a
larger store, repeat `--thread <exact-thread-id>` to make bounded per-thread or
small multi-thread plans. The complexity cap may disable visual grouping for a
very large thread; that warning is not permission to infer rejection.

## Trash an explicit rejected set

Generate a token bound to the exact IDs and Trash mode:

```bash
python3 <skill-dir>/scripts/media_cleanup.py token \
  --plan <media-plan.json> --ids <IMAGE_ID,IMAGE_ID> --trash
```

Show the IDs and exact paths, total selected size, Trash mode, bound Trash root,
and printed token. Only after the user approves that concrete set, apply the identical selection:

```bash
python3 <skill-dir>/scripts/media_cleanup.py apply \
  --plan <media-plan.json> --ids <IMAGE_ID,IMAGE_ID> \
  --confirm <approval-token> --trash
```

Report changed, skipped, and failed images plus the journal path. Files in Trash
remain recoverable but still consume disk space until Trash is emptied. Do not
empty Trash on the user's behalf unless that separate destructive action is
explicitly requested and scoped.

## Permanent deletion

Permanent mode is a separate decision, not a shortcut when Trash is
unavailable. After the preview, obtain a second explicit acknowledgement that
recovery is not promised. The helper requires `--ack-no-recovery` for both the
token and apply commands:

```bash
python3 <skill-dir>/scripts/media_cleanup.py token \
  --plan <media-plan.json> --ids <IMAGE_ID,IMAGE_ID> \
  --permanent --ack-no-recovery

python3 <skill-dir>/scripts/media_cleanup.py apply \
  --plan <media-plan.json> --ids <IMAGE_ID,IMAGE_ID> \
  --confirm <approval-token> --permanent --ack-no-recovery
```

If the plan expired, an image changed, a thread is active, a file is open, a
path escaped its sealed root, or any safety check is unavailable, accept the
skip and create a new scan. Never add a force mode.

## Platform status and product boundary

Apply support is beta on macOS and Linux; verify the host's Trash backend and
safety checks before using it. This release makes no Windows support claim;
both apply modes are hard-disabled there. Permanent mode is not a substitute
for an unsupported Trash backend.

This skill can safely review and remove explicitly rejected files that already
exist. It cannot stop Codex from generating multiple candidates, retroactively
discover user intent, or deduplicate history storage without product support.
Preventing redundant copies requires an upstream design with explicit
accept/reject state, stable references, and content-addressed blobs.
