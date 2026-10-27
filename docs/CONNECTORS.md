# Connectors: scheduled imports inside the one container

A connector keeps a project's knowledge equal to an outside source: new documents are imported, changed ones replace
their old version, and documents that disappeared are retired (archived, never deleted). They run **inside the server
process** (no worker container, no cron, no queue service): every few minutes a loop checks which connectors are due.
With several replicas a claim in the key-value store makes exactly one of them run each interval.

## Configure (`HIVE_CONNECTORS`, a JSON list)

```bash
HIVE_CONNECTORS='[
 {"name":"handbook","type":"drive","folder":"1AbCdEf...","key_file":"/run/secrets/drive.json",
  "as_user":"docs-reader@acme.com","org":"acme","owner":"connector-bot","project":"p1",
  "tags":["source/handbook"],"every_minutes":360},
 {"name":"docs","type":"github","repo":"acme/docs","ref":"main","paths":["docs/"],
  "token_env":"GITHUB_TOKEN","org":"acme","owner":"connector-bot","project":"p1"},
 {"name":"wiki","type":"urls","urls":["https://wiki.acme.com/onboarding"],
  "org":"acme","owner":"connector-bot","project":"p1"}
]'
```

Common fields: `name` (lowercase, unique), `org`, `owner` (the identity that authors the imported items; it does not need
to be a person), `project` (the project that can read them; items are written at project level, widening goes through
the normal promotion workflow), `tags`, `every_minutes` (default 360, minimum 5). A bad entry stops the server at startup.
Secrets are never in the setting: a key file path, or the *name* of an environment variable (`token_env`, `secret_env`).

| type | options | notes |
|---|---|---|
| `urls` | `urls` | each address is fetched with the URL safety rules (public hosts only unless `HIVE_INGEST_URL_ALLOW_PRIVATE`; allow-list `HIVE_INGEST_URL_HOSTS`). Re-fetched every run; unchanged content is detected by hash. |
| `github` | `repo` (`owner/name`), `ref`, `paths` (prefixes), `token_env`, `api_base` (GitHub Enterprise) | REST API, no `git` needed. Readable files only (markdown, text, html, pdf, office), no vendored/minified files, 1 MB each, 2000 files. A file is downloaded again only when its blob sha changes. A token is needed for private repositories (read-only `contents` scope). |
| `confluence` | `base_url` (`https://x.atlassian.net/wiki`), `space`, `email`, `token_env` | Pages of a space as HTML, change marker = page version. Atlassian Cloud account email + API token (read-only is enough). |
| `jira` | `base_url`, `jql`, `email`, `token_env` | Issues matching the JQL, with status, labels, description and up to 50 comments, as one note each; change marker = `updated`. |
| `notion` | `token_env`, optional `database` (id) | Every page the integration was shared with (that sharing is the access control), or one database. Blocks flattened to text, three levels deep; change marker = `last_edited_time`. |
| `sharepoint` | `tenant`, `client_id`, `secret_env`, `drive_id`, optional `folder` | A SharePoint library or OneDrive via Microsoft Graph with an Entra app (application permission `Files.Read.All`, or `Sites.Selected` for one site). Recursive (5 deep, 2000 files); change marker = eTag. |
| `drive` | `folder` (id), `key_file`, `as_user` | A Google service account with domain-wide delegation for the read-only scope `https://www.googleapis.com/auth/drive.readonly`, reading as `as_user`. Sub-folders (5 deep, 2000 files), Docs and Slides exported as text, Sheets as CSV, PDFs/Office/text downloaded; other types skipped. Change marker: checksum or modified time. |

## Safety rules

- **Nothing is retired on a bad run.** If the listing fails, or any document failed to download, no document is
  retired. If more than half of the documents (and at least 5) vanish at once, nothing is retired either (an empty
  listing from a revoked permission must not wipe the knowledge base); `hive connectors run --force` overrides.
- **Changed documents** are ingested again and the previous parts are archived (still in history, hidden from search).
- Imported text goes through the same sanitising as every write (secrets masked, hidden characters removed, prompt
  injection flagged with `flag/injection`), and the items carry `source` = `connector:<name>:<id>`.
- A people-edited imported item is not protected from the next sync replacing the document: edit the source.

## Operate

`hive connectors list | status | run [name] [--force]` (run syncs once, now). Each run writes a `connector.sync` audit
event with the counts. In a deployment without the loop (`HIVE_RUN_JOBS=false`) use `hive connectors run` from a
scheduled task instead.
