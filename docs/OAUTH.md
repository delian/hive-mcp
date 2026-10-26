# Sign-in for claude.ai, ChatGPT, Gemini and other assistants (OAuth 2.1 + Google)

Interactive assistants connect with OAuth. The server is its own **authorization server** (the MCP SDK serves the protocol
endpoints) and delegates *who you are* to **Google Workspace**. A small **user directory** says what each person may do.

```
assistant --(1 register, 2 /authorize + PKCE)--> hive --(3 redirect)--> Google sign-in
assistant <--(8 code)-- hive consent page <--(5 verified ID token, 6 directory check)-- hive <--(4 code)-- Google
assistant --(9 /token + PKCE verifier)--> hive --> access token (1 h) + rotating refresh token (30 d)
```

## Setup
1. In Google Cloud Console create an **OAuth client of type "Web application"**. Authorised redirect URI:
   `https://<your-public-host>/oauth/google/callback`.
2. Configure the server (environment variables):
   ```
   HIVE_PUBLIC_URL=https://hive.example.com          # the externally visible URL (tunnel / load balancer); must be https
   HIVE_GOOGLE_CLIENT_ID=...apps.googleusercontent.com
   HIVE_GOOGLE_CLIENT_SECRET=...
   HIVE_GOOGLE_ALLOWED_DOMAINS='["example.com"]'     # your Workspace domain(s); empty means nobody can sign in
   HIVE_SECRET_KEY=...                               # optional; otherwise generated into <data_dir>/secret.key
   ```
3. Tell the server who may sign in and what they belong to (Google proves identity only):
   ```bash
   hive user add alice@example.com --org example --role member --team rf --project radar-x
   hive user list
   hive user remove alice@example.com        # takes effect on live tokens at once
   ```
4. Add the connector in the assistant using `https://<your-public-host>/mcp`. It discovers everything else.

Static bearer tokens (`hive token create`) keep working next to OAuth, for pipelines and the CLI.

## What a sign-in must pass
- Google's ID token: RS256 signature against Google's published keys, issuer, audience (our client id), expiry, and the
  per-login nonce. Anything else is refused and the assistant never receives a code.
- A **verified email** of an **allowed domain** that Google reports as a **Workspace account of that domain** (`hd`), and an
  entry in the user directory that is not disabled. Lookalike domains and personal accounts using a work address fail.
- A **consent page** shows the application name and where it will be sent. Dynamic registration is open by design
  (assistants register themselves), so without consent anyone could register a client and receive a signed-in user's code.

## Security properties (each is covered by tests that were checked by deliberately breaking the code)
- PKCE (S256) is mandatory; redirect URIs must match the registered ones exactly. Registration refuses script-like
  schemes and non-loopback `http`.
- Authorization codes are single use (5 min); replaying one revokes every token it produced.
- Refresh tokens rotate on every use; presenting an already-used one revokes the whole session ("family").
- Tokens are stored only as hashes. Access tokens are opaque and bound to this server's URL (RFC 8707).
- Permissions are read from the directory on **every request**: removing, disabling or demoting a user takes effect at once.
- The browser is bound to its login by a signed, `HttpOnly`, `SameSite=Lax` cookie (defence against login CSRF); the consent
  form has its own CSRF token; pages cannot be framed and send no scripts.
- Clients can revoke only their own tokens (`/revoke`, RFC 7009, works for secret-less public clients).

## Consent phishing and look-alike apps
Anyone can register a client (that is how claude.ai/ChatGPT/Gemini connect) and send a user a sign-in link; the consent page
then shows an app name the app chose itself. The page also shows the host the user will be sent to, but a careless "Allow"
would hand that app a 30-day refresh token. Close this by setting `HIVE_OAUTH_REDIRECT_HOSTS` to the hosts of the assistants
you use (for example `["claude.ai","chatgpt.com"]`): registration of any other redirect host is then refused.

## Who a sign-in belongs to
The first successful Google sign-in of a directory user pins their Google account id; a later sign-in with the same email
but another Google account (an address deleted and reassigned) is refused. Editing the user with `hive user add` clears the pin.

## Limits and decisions to know about
- Consent is remembered per user + client + redirect URI + scopes for `HIVE_CONSENT_REMEMBER_DAYS` (default 30; 0 = ask every
  time). The Google sign-in and the directory check still happen on every login.
- Open client registration can be spammed. Registrations that never complete a sign-in expire after 30 days; put a rate limit
  in front (Azure Front Door / WAF) for production.
- Confidential-client secrets (clients that register with a secret) are stored as the SDK requires, in the database.
- Teams and projects come from the directory file, or from Workspace groups with [group sync](GROUPS.md).
- Google is only contacted at sign-in; later Google-side suspension is not noticed until the directory entry is changed.
- The SDK's `/revoke` is replaced by ours because its request model rejects secret-less public clients.
