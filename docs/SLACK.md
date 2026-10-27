# Slack: ask and save from where the team talks

Two things, served by the same process as everything else (two extra HTTP routes, no bot container):

- **`/hive <question>`** searches the knowledge base. **`/hive save <text>`** keeps a note.
- A message shortcut **Save to Hive** keeps any message as a note.

Everything is **private to the asker**: replies are ephemeral (only they see them), search runs with *their* access,
and saved notes are private to them (share later from their agent with `kb_share`; wider sharing keeps needing an
approver). Saved text goes through the usual sanitising (secrets masked, hidden characters removed, prompt injection
flagged). Replies escape Slack markup, so a stored note cannot create `@channel` pings or disguised links.

## Set up (about ten minutes)

1. api.slack.com/apps, **Create New App** from scratch, pick your workspace.
2. **Slash Commands**: command `/hive`, request URL `https://<public-url>/slack/commands`.
3. **Interactivity & Shortcuts**: on, request URL `https://<public-url>/slack/interactions`; **Create New Shortcut**,
   type *On messages*, name "Save to Hive", callback id **`hive_save`**.
4. **OAuth & Permissions**: bot scopes `commands` and `users:read.email`; install to the workspace; copy the *Bot User
   OAuth Token*. (Skip the token and the scope if you prefer the fixed map below.)
5. **Basic Information**: copy the *Signing Secret*. The workspace id (`T...`) is in the browser URL of the workspace or
   in `auth.test`.
6. Configure the server:

```bash
HIVE_SLACK_SIGNING_SECRET=...        # verifies every request
HIVE_SLACK_TEAM_ID=T0ACME            # requests from any other workspace are ignored
HIVE_SLACK_BOT_TOKEN=xoxb-...        # finds each person's email (users.info), cached an hour
# or, without a token: HIVE_SLACK_USERS='{"U123ABC": "ann@acme.com"}'
```

## Managing it at run time (no restart)

The environment variables above are only the starting point. Once an admin saves settings, the stored ones win:

- **Admin page** `/admin/integrations`: enable or disable, workspace id, the signing secret and bot token (write-only:
  shown as "stored", never displayed again, encrypted at rest with a key derived from `HIVE_SECRET_KEY`), who may be
  mapped without a bot token, which channels share into which project, and whether `@mentions` are answered.
- **Admin chat tools** (admins only, audited): `kb_admin_integration(name)` shows the settings (never secrets),
  `kb_admin_integration_set` changes enabled/workspace/mentions/users, `kb_admin_integration_map(name, channel, project)`
  maps or unmaps a channel. **Secrets are refused in tool arguments on purpose**: tool calls appear in chat transcripts.
- Every change is an `integration.update` audit event (which fields, never values). Other replicas pick a change up within
  5 seconds.

**Channel to project.** With `channel-id=project` mappings, `/hive save` and *Save to Hive* in that channel save into the
project (visible to its members) instead of privately, **only for people who are members of that project** (the mapping
never widens anyone's access), and the reply says where it went. Unmapped channels stay private.

**@mentions** (off by default; enable per workspace). Add the bot to a channel, subscribe the app to the `app_mention`
event with request URL `https://<public-url>/slack/events`, add scope `app_mentions:read` and `chat:write`. The bot answers
in a thread, and because a channel is an audience it quotes **only org-wide knowledge plus the notes of the channel's mapped
project**; everything else is answered privately by `/hive`. Bots, edits and other workspaces are ignored; retries of the
same event are answered once.

## Who is asking

The Slack user's confirmed email is looked up and must belong to a person in the user directory (`hive user add`);
they act with that person's roles and projects. Not in the directory, disabled, a guest, a bot, or from another
workspace: they get a short "could not match your account" reply and nothing else. Disabling someone in the
directory cuts them off at once (the email-to-person mapping is cached, the directory check is not).

## Security properties

- Every request is checked against Slack's signature (HMAC-SHA256 of `v0:<timestamp>:<body>`) **before** the body is
  read; requests older than five minutes are refused and each signature is accepted once (a captured request cannot
  be replayed, also across replicas).
- Bodies over 64 KB are refused; the routes are rate limited like the login routes (`HIVE_RATE_LIMIT_AUTH`).
- Follow-up messages go only to `https://hooks.slack.com/...` (the `response_url` Slack supplies).
- Not built: Microsoft Teams (it needs an Azure Bot Service registration and a different token flow), threads/mentions
  (`app_mention` events) and posting into channels; replies are always private to the asker on purpose.
