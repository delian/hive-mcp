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
3. The Teams app package: `HIVE_PUBLIC_URL=https://<public-url> hive teams-package --app-id <application id> --out hive-teams.zip`
   writes a zip with the manifest (bot for personal chats, teams and group chats, plus the message extension below) and
   two icons. Upload it in Teams (Apps, Manage your apps, Upload an app) or in the Teams admin center. The privacy and terms
   links in it point at your server's address: replace them in the manifest before publishing beyond your own tenant.
4. In Hive, `/admin/integrations`: tenant id, application id, app password, enable. Add people to the user directory
   (`hive user add`): they are matched by the email Teams reports for them (looked up through the bot connector, cached an
   hour), or pin `aad-object-id=email` lines in the page for people the lookup cannot find.

## Message extension
Two commands, declared in the package:
- **Search Hive** (compose box and the Teams search bar): type a query, get result cards to insert into the message.
  The card goes into a conversation in front of everyone there, so, as for channel answers, only organisation-wide
  knowledge and the notes of the project the conversation is mapped to are offered; private and project notes stay with
  the bot in your personal chat. Card text is escaped (no links, mentions or formatting from stored text).
- **Save to Hive** (the `...` menu of any message): saves the message as a note **private to you**, with the author's name
  and a link to the message as its source, and tells you in a small dialog. There is no form: saving happens as the dialog
  opens (the note is keyed to the message, so a retry or second click saves nothing twice). Move it to a project later
  with `kb_request_promotion`.
These arrive as `invoke` activities, are verified like every other activity, answered in the HTTP response (Teams allows
five seconds), and are not de-duplicated (a retry is simply answered again). The same tenant and directory checks apply.

## Security properties
- Every request must carry a Bot Framework JWT: signature checked against the published keys (RS256 only; refetched on an
  unknown key), issuer `https://api.botframework.com`, audience = your application id, expiry, and the `serviceurl` claim
  must equal the activity's `serviceUrl`. Anything else gets 401. Activities are accepted once (redelivery is ignored).
- Replies carry your bot's access token, so they go only to `https` hosts under `botframework.com`, `botframework.us` or
  `trafficmanager.net`, and the URL path parts are escaped.
- Only your tenant's activities are answered; the sender must be in the user directory and not disabled. Bodies over 256 KB
  are refused.

## Not built
- Multi-tenant bots (Microsoft no longer allows creating them) and the Bot Framework Emulator's tokens.
- A form for choosing a project when saving a message, link unfurling, and Outlook support for the extension.
- Verified only against locally signed tokens and mocked HTTP, plus Microsoft's documented request and response shapes
  (manifest 1.17, `composeExtension/query`, `composeExtension/fetchTask`), not a real tenant. Teams is strict about
  manifests: if the upload is refused, the message names the field.
