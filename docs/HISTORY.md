# Item history

Every edit and every move (`kb_update`, `kb_share`, promotions, tag changes, restores) keeps the version it
replaces, inside the same database transaction, so history cannot miss a change. The newest 50 previous versions of
each item are kept; older ones are dropped. Edits overwrite in place, so history is the only record of earlier text.

| Tool | |
|---|---|
| `kb_history(id)` | versions newest first: revision, title, when written, who replaced it and when, where the item lived then |
| `kb_version(id, revision)` | one version in full |
| `kb_diff(id, from_revision, to_revision?)` | unified diff of title and body (default: up to the current version) |
| `kb_restore(id, revision, expected_revision)` | make the item say what that version said; creates a new revision |

**Privacy rule:** a version can be read only by someone who could have read the item *as it was at that revision*.
A draft written while an item was private stays private to its author after the item is shared (members of the
project see history from the moment of sharing), and if an item becomes private again nobody else sees any of it.
Restoring needs edit rights and a version you may read. Purging an item deletes its history (see RETENTION.md).
Search always indexes only the current version.
