# Microsoft Teams

A Teams bot served by the same process (route `POST /teams/messages`; no extra container). It is managed like Slack,
at run time: admin page `/admin/integrations`, or the admin chat tools `kb_admin_integration`,
`kb_admin_integration_set` and `kb_admin_integration_map` with `name="teams"`. The app password can only be entered on
the admin page (tool arguments appear in chat transcripts, so they are refused for secrets).

## Behaviour
- **Personal chat with the bot**: ask a question (search, your own access) or `save <text>` (a private note).
- **Channels and group chats**: off until an admin ticks "answer when mentioned". The audience is everyone in the
  channel, so the bot quotes **only organisation-wide knowledge and the notes of the project the channel is mapped to**
  (`conversation-id=project`), and a `save` there echoes nothing it saved. Saving into the mapped project works for members
  of that project only; otherwise the note is private to the sender.
- Replies are plain text (no markdown or HTML to inject into).

## Set up
1. Azure portal, **App registrations**: new registration, **single tenant**. Note the *Application (client) id* and the
   *Directory (tenant) id*; create a client secret.
2. Azure portal, **Azure Bot** resource: type single-tenant, use that app id, messaging endpoint
   `https://<public-url>/teams/messages`; enable the **Microsoft Teams** channel.
3. A Teams app manifest (Teams Developer Portal) with that bot id, scopes personal, team and group chat; upload or publish
   to your organisation.
4. In Hive, `/admin/integrations`: tenant id, application id, app password, enable. Add people to the user directory
   (`hive user add`): they are matched by the email Teams reports for them (looked up through the bot connector, cached an
   hour), or pin `aad-object-id=email` lines in the page for people the lookup cannot find.

## Security properties
- Every request must carry a Bot Framework JWT: signature checked against the published keys (RS256 only; refetched on an
  unknown key), issuer `https://api.botframework.com`, audience = your application id, expiry, and the `serviceurl` claim
  must equal the activity's `serviceUrl`. Anything else gets 401. Activities are accepted once (redelivery is ignored).
- Replies carry your bot's access token, so they go only to `https` hosts under `botframework.com`, `botframework.us` or
  `trafficmanager.net`, and the URL path parts are escaped.
- Only your tenant's activities are answered; the sender must be in the user directory and not disabled. Bodies over 256 KB
  are refused.

## Not built
- A message extension / message action ("Save to Hive" on any message, like Slack's shortcut) needs `invoke` handling and
  manifest `composeExtensions`; not implemented. Say `save <text>` to the bot instead.
- Multi-tenant bots (Microsoft no longer allows creating them) and the Bot Framework Emulator's tokens.
- Verified only against locally signed tokens and mocked HTTP, not a real tenant.
