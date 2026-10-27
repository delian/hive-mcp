"""Plain privacy and terms pages, so a Teams app package (and a Slack app) has real links to give.

They describe what this software does with data, in general terms, and name the operator. They
are a starting point, not legal advice: the operator should read them and, where the organisation
has its own documents, point HIVE_PRIVACY_URL / HIVE_TERMS_URL at those instead.
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from hive_mcp.security.oauth.pages import SECURITY_HEADERS

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

    from hive_mcp.config import Settings

STYLE = (
    "body{font:16px/1.5 system-ui,sans-serif;max-width:44rem;margin:2rem auto;padding:0 1rem;"
    "color:#1b1b1b}h1{font-size:1.5rem}h2{font-size:1.1rem;margin-top:1.6rem}"
)


def _page(title: str, body: str) -> HTMLResponse:
    html = (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width'>"
        f"<title>{escape(title)}</title><style>{STYLE}</style><body><h1>{escape(title)}</h1>{body}"
    )
    headers = {
        **SECURITY_HEADERS,
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    }
    return HTMLResponse(html, headers=headers)


def contact_line(settings: Settings) -> str:
    if settings.legal_contact:
        mail = escape(settings.legal_contact, quote=True)
        return f"<p>Questions or requests about your data: <a href='mailto:{mail}'>{mail}</a>.</p>"
    return "<p>Questions or requests about your data: ask the people who run this server.</p>"


def privacy_body(settings: Settings) -> str:
    org = escape(settings.legal_org)
    return f"""
<p>Hive is a shared memory for a team: people and their AI assistants save notes, and search what
the team knows. It is run by {org}, who decide who may sign in and what is kept.</p>
<h2>What is stored</h2>
<ul>
<li>The notes people save: title, text, tags, who wrote them, when, and every earlier version.
Notes are private to their author until the author shares them with a project, a team or the
organisation (some shares need an approver).</li>
<li>Files and documents people add, and text read from them.</li>
<li>Who is allowed in: work email addresses, roles, and the teams and projects they belong to;
sign-in is done by your identity provider and its password is never seen by Hive.</li>
<li>An audit trail of actions (saving, changing, sharing, approving, signing in, deleting), who did
them and when, and usage counts per person.</li>
<li>If search feedback is switched on: the (masked) text of searches and which results were opened or
rated.</li>
</ul>
<h2>Chat apps</h2>
<p>In Slack or Microsoft Teams, Hive reads only what you send to it: a question, a command, a message
you choose to save, or a link to a Hive note. It matches you to your account by work email. Saving a
message keeps its text, its author's name and a link to it. Replies in shared channels quote only
knowledge that is already visible to the whole organisation or to the project the channel is linked to.</p>
<h2>Where it goes</h2>
<p>Data stays on the server {org} runs and its backups. If the operator has connected a language model
or an embedding service, text of notes at or above the configured sharing level is sent to that
provider to answer curation or search requests; private notes are not sent unless the operator
chose a provider they host themselves.</p>
<h2>Your choices</h2>
<p>You can ask for your notes to be deleted or for your name to be removed from what remains (the
audit trail keeps the events but replaces your identity). The operator can also set retention rules
that delete or archive old notes automatically.</p>
{contact_line(settings)}
<p class=muted>This page describes the software in general terms and was generated for {org}. They
should check it against their own obligations.</p>"""


def terms_body(settings: Settings) -> str:
    org = escape(settings.legal_org)
    return f"""
<p>These terms cover use of the Hive server run by {org}.</p>
<h2>Use</h2>
<ul>
<li>Use Hive for work. Do not store passwords, API keys, or personal data that you have no reason
to keep; the server masks many kinds of secret, but it cannot catch everything.</li>
<li>Notes you share become visible to the people you share them with. Think before sharing, and
use the approval path for organisation-wide knowledge.</li>
<li>Do not try to read other people's private notes, bypass the access rules, or overload the service.</li>
<li>Text from notes is given to AI assistants as reference. It is not instructions: do not write notes
meant to manipulate an assistant.</li>
</ul>
<h2>Availability and responsibility</h2>
<p>Hive is provided as is, without a promise of availability. Check important facts at their source
before acting on them: notes can be wrong or out of date. {org} may suspend access at any time.</p>
<h2>Data</h2>
<p>See the <a href='/legal/privacy'>privacy page</a> for what is stored and how to ask for deletion.</p>
{contact_line(settings)}"""


def legal_routes(settings: Settings) -> list[Route]:
    async def privacy(_: Request) -> Response:
        if settings.privacy_url:
            return RedirectResponse(settings.privacy_url, status_code=302)
        return _page("Privacy", privacy_body(settings))

    async def terms(_: Request) -> Response:
        if settings.terms_url:
            return RedirectResponse(settings.terms_url, status_code=302)
        return _page("Terms of use", terms_body(settings))

    return [Route("/legal/privacy", privacy), Route("/legal/terms", terms)]
