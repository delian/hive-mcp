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
Three things, declared in the package:
- **Search Hive** (compose box and the Teams search bar): type a query, get result cards to insert into the message. The card
  goes into a conversation in front of everyone there, so, as for channel answers, only organisation-wide knowledge and the
  notes of the project the conversation is mapped to are offered. Card text is escaped (no links, mentions or formatting from
  stored text). With the admin UI on, each card has an **Open in Hive** button.
- **Save to Hive** (the `...` menu of any message): someone who belongs to no project gets a one-click save of a note
  **private to them**. Someone in projects gets a small dialog with a dropdown, *Private to me* (the default) or one of their
  projects, and a **Save** button. The message is held for ten minutes between opening the dialog and pressing Save, only for
  that person; saving twice does nothing twice; the same message can be kept in another place as another note. The note
  carries the author's name and a link to the message as its source. Move a note later with `kb_request_promotion`.
- **Link unfurling**: paste a link to a note (`https://<your server>/admin/n/<id>`, the "Open in Hive" address) and Teams turns it
  into a card. As in search, the text of a note is quoted only if everyone in the conversation may see it (organisation-wide, or
  the conversation's mapped project, and active); for any other note, a missing one, or one you cannot read, the card is the
  same bare "Hive note, open it in Hive" card, so it reveals nothing. The card is marked not to be cached, because Teams would
  otherwise show it to others for 30 minutes.
These arrive as `invoke` activities, are verified like every other activity, answered in the HTTP response (Teams allows five
seconds), and are not de-duplicated (a retry is simply answered again). The same tenant and directory checks apply.

### Note pages
Links open `/admin/n/<id>`, a read-only page of the admin UI (so it exists only with `HIVE_ADMIN_UI=true`; the package
adds link unfurling only then, or when you pass `--link-unfurling`). **Any person in the directory** may sign in there, by
Google/Entra/OIDC sign-in or a pasted token, and read the notes they could read through the tools; someone who is not signed
in is sent to the sign-in page and brought back to the note. Everything else in the admin UI still needs its own role.

### Privacy and terms pages
Teams wants a privacy and a terms link in the package. The server serves plain pages at `/legal/privacy` and `/legal/terms`
(what is stored, chat apps, where data goes, how to ask for deletion; use, availability) naming the operator:
`HIVE_LEGAL_ORG`, `HIVE_LEGAL_CONTACT`. They are a starting point and **not legal advice**: read them, and where your
organisation has its own documents set `HIVE_PRIVACY_URL` / `HIVE_TERMS_URL` and the pages redirect there (and the package
uses them).

## Security properties
- Every request must carry a Bot Framework JWT: signature checked against the published keys (RS256 only; refetched on an
  unknown key), issuer `https://api.botframework.com`, audience = your application id, expiry, and the `serviceurl` claim
  must equal the activity's `serviceUrl`. Anything else gets 401. Activities are accepted once (redelivery is ignored).
- Replies carry your bot's access token, so they go only to `https` hosts under `botframework.com` or `botframework.us`, or
  exactly `smba.trafficmanager.net`, and the URL path parts are escaped.
- Only your tenant's activities are answered; the sender must be in the user directory and not disabled. Bodies over 256 KB
  are refused.

## Not built
- Multi-tenant bots (Microsoft no longer allows creating them) and the Bot Framework Emulator's tokens.
- Zero-install unfurling (`supportsAnonymizedPayloads`): the app has to be installed for links to unfurl. Outlook support.
- Verified only against locally signed tokens and mocked HTTP, plus Microsoft's documented request and response shapes
  (manifest 1.17, `composeExtension/query`, `fetchTask`, `submitAction`, `queryLink`), not a real tenant. Teams is strict about
  manifests and cards: if the upload is refused or a dialog does not open, the message names the field.
