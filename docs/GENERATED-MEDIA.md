# Generated-media policy

Generated media is not an ordinary cache. It may be a rejected variation, a chosen asset, evidence in task history, or the only remaining copy of useful work. Codex Project Hygiene therefore treats every discovered image as protected `unknown` data until the user explicitly rejects its exact ID.

## What the audit found

A read-only audit of one real Codex installation found:

- 30 generated-image task directories;
- 631 PNG files using approximately 1.046 GiB;
- between 1 and 117 images per task directory;
- no dependable machine-readable accepted, selected, kept, rejected, discarded, or deleted state in the inspected session, database, and state records;
- only one exact SHA-256 duplicate pair, worth approximately 1.7 MB;
- 630 of 631 image paths referenced by task history, which still does not indicate whether the user accepted the image.

These are observations from one installation, not universal benchmarks. They demonstrate why “delete duplicates” alone does not solve the retention problem and why a history reference cannot substitute for user intent.

The same audit observed at least three storage layers: roughly 1.123 GB of PNG files, 2.292 GB of repeated image/base64 material in session JSONL, and 0.376 GB in database storage—approximately 3.53 GiB combined. Editing JSONL or SQLite to reclaim that amplification could desynchronize task history and is outside this skill's safety boundary.

## What similarity means

The local gallery can show bounded, lossy thumbnails for:

- exact groups based on full SHA-256 content;
- perceptual groups based on a 64-bit difference hash;
- perceptual groups only within the same task/thread, reducing unrelated cross-task matches.

Neither group is a deletion rule. Perceptual hashes can group distinct compositions with similar colors or structure. Exact copies can still occupy two separately referenced history paths. The gallery is a visual review aid; all checkboxes start unchecked, and only the resulting exact ID list can enter approval. A thumbnail can hide text or fine differences, so inspect the displayed exact path with a trusted resource-limited viewer whenever detail affects the choice.

## Safe review workflow

1. Create a new plan and optional local gallery:

   ```bash
   python3 scripts/media_cleanup.py scan \
     --plan /private/path/codex-media-plan.json \
     --gallery /private/path/codex-media-gallery.html
   ```

   A gallery is limited to 200 images and 32 MiB of encoded thumbnails. For a
   larger collection, create separate plans with one or more repeated
   `--thread THREAD_ID` filters. Running without `--gallery` can still create a
   complete read-only inventory.

2. Open the gallery locally. Treat every image as kept unless the user explicitly rejects that exact file. Do not infer rejection from “all but latest,” a repeated prompt, lower resolution, an older timestamp, or cluster membership.

3. Build the comma-separated list of exact IDs in the gallery. Show the user the sealed plan, count, estimated size, and Trash mode.

4. Derive an operation-bound token:

   ```bash
   python3 scripts/media_cleanup.py token \
     --plan /private/path/codex-media-plan.json \
     --ids ID_1,ID_2 \
     --trash
   ```

5. Only after explicit approval, apply the same plan, IDs, mode, and token:

   ```bash
   python3 scripts/media_cleanup.py apply \
     --plan /private/path/codex-media-plan.json \
     --ids ID_1,ID_2 \
     --trash \
     --confirm APPROVAL_TOKEN
   ```

Apply checks the image's full SHA-256, allocated size, filesystem identity, direct task-directory location, mount boundary, thread activity, and open handles again. A changed or unverifiable item is skipped. The approval token also binds the exact platform and canonical Trash backend/root.

Permanent deletion is available only with `--permanent --ack-no-recovery` on both token and apply. It is intentionally harder to invoke and should not be the default.

## Privacy

The plan contains absolute paths and hashes. The gallery contains escaped paths and embedded bounded PNG thumbnails; it does not load original images through `file:` URLs. Corrupt, unsupported, or disabled previews become placeholders. An over-budget gallery fails closed and must be split with narrower `--thread` filters. The optional `ffmpeg` decoder receives bounded input and its stdout/stderr are strictly capped. The helper performs no upload or network request. Store plans and galleries in a private directory, delete them when no longer needed, and never attach a real gallery, plan, session, or database to a public issue.

## Hard limit of a local skill

This workflow can review existing image files. It cannot stop Codex from writing multiple generations, remove repeated base64 safely, or know which variation a user dismissed. Solving those problems requires Codex itself to record explicit lifecycle state and store image content once. See [Upstream proposal](UPSTREAM-PROPOSAL.md).
