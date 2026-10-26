# Google Workspace group sync

Teams, projects, approvers and roles can follow Workspace groups instead of being typed with `hive user add`.
The sync reads group members and updates the user directory (`users.json`); everything else (tokens, tools)
already reads the directory, so changes apply to live sessions at once.

## One-time setup in Google
1. Create a **service account** (Cloud console) and a JSON key. Note its numeric *client ID*.
2. In the Admin console: *Security > API controls > Domain-wide delegation > Add new*: the client ID and the scope
   `https://www.googleapis.com/auth/admin.directory.group.member.readonly`.
3. Pick a Workspace admin the service account may act as (`HIVE_GROUP_SYNC_ADMIN`); only group membership is read.

## Configuration
| Setting | Meaning |
|---|---|
| `HIVE_GROUP_MAP_FILE` | path of the mapping (below); setting it turns the feature on |
| `HIVE_GROUP_SYNC_KEY_FILE` | the service account JSON key (keep it as a secret file, mode 600) |
| `HIVE_GROUP_SYNC_ADMIN` | the admin to impersonate |
| `HIVE_GROUP_SYNC_MINUTES` | run in the server every N minutes; `0` = only with `hive sync-groups` |

```json
{
  "org_id": "acme",
  "auto_create": true,
  "default_role": "member",
  "disable_missing": false,
  "groups": {
    "eng@acme.com":        {"teams": ["eng"]},
    "apollo@acme.com":     {"projects": ["apollo"]},
    "eng-leads@acme.com":  {"approves": ["team:eng"], "role": "editor"},
    "kb-admins@acme.com":  {"role": "admin"}
  }
}
```

## Rules
- **The sync owns the values the map mentions**, nothing else. A user in `eng@` gets team `eng`; a user who leaves it
  loses `eng`. Teams, projects and approver scopes you set by hand that the map never mentions are left alone.
- **Roles:** if any group defines a role, a user who is in at least one mapped group gets the highest role among their
  groups, or `default_role` when none of theirs defines one (so leaving `kb-admins@` revokes admin). People in *no*
  mapped group keep their role (the bootstrap admin is safe).
- `auto_create` adds Workspace members missing from the directory (only for `HIVE_GOOGLE_ALLOWED_DOMAINS`), in `org_id`.
- `disable_missing` disables directory users who are in no mapped group; admins are never disabled automatically.
  A disabled user who returns to a group is re-enabled.
- **Nested groups** count (derived membership). Suspended Workspace users and non-user members are ignored.
- **All-or-nothing:** if any group cannot be read, or returns no members (usually a typo, and it would strip everyone of
  that group's access), nothing is changed. `--allow-empty` overrides the second case deliberately.

## Use
```bash
hive sync-groups --dry-run   # print what would change
hive sync-groups             # apply
```
Every applied change is logged (`groups.sync action=... user=...`). The in-server loop (`HIVE_GROUP_SYNC_MINUTES`) logs
failures and retries next round. With several replicas each runs it; the result is the same, only the work is repeated.
