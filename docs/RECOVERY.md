# Recovery guide

## Before applying

Use Trash mode. Confirm that irreplaceable projects and assets have a backup independent of the machine being cleaned. Read the exact plan, IDs, estimated size, and mode; do not approve a wildcard summary.

Keep the plan and result/journal until the operation is verified. They can contain sensitive paths, so do not upload them publicly.

## Restore from Trash

Trash mode uses same-filesystem atomic moves. It does not reclaim disk space until Trash is emptied.

1. Do not empty Trash.
2. Read the execution result or append-only journal beside the plan and locate the exact item ID and recorded Trash path. Every Trash attempt records and flushes its expected destination before the atomic rename.
3. Use the operating system's **Put Back** or restore action when available.
4. If the original path has been recreated, do not overwrite it. Restore the trashed item to a separate staging directory, compare both trees, and decide manually.
5. Re-run the application's ordinary install or build command only after source and configuration are verified.

On Linux, project and generated-media Trash moves include FreeDesktop `.trashinfo` metadata. Use the desktop environment's Trash UI rather than manually editing that metadata. Trash behavior remains platform- and filesystem-sensitive during beta; inspect the printed destination before applying.

If an interruption occurs after an `attempt` event but before an item success
event, inspect that attempt's `expected_trash_path`. Destination names include
the exact item ID. Do not assume the original or destination is absent: verify
both without overwriting either, then use the operating system's restore UI or
stage the recovered item at a separate path.

## Partial or failed apply

Apply can legitimately finish with changed, skipped, and failed items in one run. A skip means a precondition changed or could not be verified. It is not permission to bypass the helper.

- Preserve the result/journal.
- Verify each changed item and Trash destination.
- Leave skipped items untouched.
- Resolve active processes or filesystem issues.
- Create a fresh plan and request a fresh approval; do not reuse an old token.

If an interruption leaves a hidden adjacent name beginning with `.codex-disk-cleanup-deleting-` or `.codex-rejected-deleting-`, stop. Do not delete it. Record its path, avoid recreating or modifying the original target, and report the recovery case with sanitized synthetic details. It may be an atomically quarantined original.

## Permanent mode

There is no application-level undo for permanent deletion. The helper attempts to restore an atomic tombstone when revalidation or removal fails, but a successful permanent operation is final from the tool's perspective.

If permanent deletion was a mistake:

1. Stop write-heavy activity on that filesystem.
2. Check independent backups, filesystem snapshots, and version-control remotes.
3. For source or unique assets, consider professional recovery before installing dependencies or rebuilding into the same volume.
4. Do not edit Codex SQLite or session JSONL in an attempt to recreate missing generated media.

## Rebuilding reproducible data

Rebuild with the project's own locked workflow, not a command copied from an untrusted plan. Examples include the repository's documented package-manager install, build, or cache warm-up commands. Review lockfile changes before accepting them.

Generated images are not considered reproducible: the same prompt and model may not recreate the same pixels. Restore them from Trash or backup instead of regenerating and assuming equivalence.
