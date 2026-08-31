# Upstream proposal: generated-media lifecycle and storage

## Problem

Current local state can contain many generated variations without a reliable accepted/rejected marker. Image bytes may also appear in the PNG tree, session JSONL, and database storage. A local cleaner cannot safely infer which variation the user dismissed, and rewriting history stores risks desynchronizing Codex.

The durable solution belongs in Codex's generation and history pipeline, not in filename heuristics.

## Goals

- Store identical image bytes once.
- Record explicit user intent without treating silence as rejection.
- Keep task history consistent when content is retired.
- Offer predictable storage budgets and recoverable cleanup.
- Make disk usage visible before it becomes an emergency.
- Preserve local privacy and avoid retention telemetry.

## 1. Content-addressed blob store

Write each generated binary to a blob keyed by a versioned cryptographic content hash. Task history and the database store a blob ID plus metadata, not repeated base64 payloads or independent authoritative copies.

Blob creation should be atomic. Reference updates and metadata commits should be transactional. Exact deduplication occurs at write time; perceptual similarity must never merge blobs because distinct images can look alike.

## 2. Explicit lifecycle state

Each generation result starts as `unreviewed`, which is protected. Supported transitions should be user-visible and event-backed:

- `unreviewed -> kept` when the user selects, exports, inserts, pins, or explicitly keeps it;
- `unreviewed -> rejected` only through an explicit Reject/Delete action or a clearly labeled “discard remaining variations” action;
- `rejected -> kept` through Undo or restore during a grace period;
- any durable artifact reference or explicit keep event wins over a conflicting rejection.

Closing a window, generating another variation, age, prompt similarity, and being “not the newest” must not silently mean rejected.

## 3. Recoverable retention policy

Offer an opt-in policy with understandable choices:

- keep all generations;
- move explicitly rejected generations to an app Trash after a configurable delay;
- prompt when generated-media storage exceeds a user-set budget;
- preview exact items and expected savings before collection.

Rejected blobs should enter a grace-period Trash first. Permanent garbage collection runs only after the grace period and only when the blob has no kept, unreviewed, exported, pinned, or history-required references.

## 4. Transactional reachability garbage collection

Use a mark-and-sweep design over blob references:

1. Begin from kept, unreviewed, pinned, exported, active-task, and retention-required roots.
2. Mark reachable blob IDs in one consistent database snapshot.
3. Produce a user-readable dry-run manifest.
4. Revalidate database generation and reference counts.
5. Atomically move unreferenced rejected blobs to app Trash.
6. Journal every state transition and support Undo.
7. Permanently remove expired Trash blobs in a separate operation.

History records should reference stable blob IDs. Garbage collection must never patch arbitrary JSONL lines in place.

## 5. Storage dashboard

Expose local totals by layer and state:

- kept, unreviewed, rejected, and Trash media;
- exact physical blob bytes versus logical references;
- session/history and database overhead;
- expected immediately reclaimable space;
- oldest and largest generation batches.

The dashboard should explain that clones, compression, snapshots, and Trash can make estimated and actual reclaimed bytes differ.

## 6. Migration

Migration from path/base64 storage should be resumable and fail closed:

- ingest one object at a time into the content-addressed store;
- verify the new blob hash before adding references;
- keep old data until all references commit successfully;
- record a migration journal and support restart;
- never infer accepted/rejected state during migration—legacy items become `unreviewed`;
- delete old copies only in a later previewed, recoverable phase.

## Acceptance criteria

An upstream implementation is ready when it can demonstrate:

- one physical blob for multiple exact references;
- no automatic deletion based on visual similarity;
- no deletion of `unreviewed` or `kept` content;
- crash-safe generation, migration, Trash, restore, and garbage collection;
- concurrent task writes cannot race collection;
- history remains readable before, during, and after collection;
- disk usage and every selected blob are previewable;
- cleanup is local-only unless the user separately opts into sync;
- adversarial tests cover stale manifests, reference races, corrupted blobs, and interrupted migration.

Until those conditions exist upstream, Codex Disk Cleanup will keep generated-media cleanup explicit, per-file, review-only, and separate from Codex history stores.
