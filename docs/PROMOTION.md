# Promotion (sharing beyond a project)

Items are private. A member may share to a project they belong to (`kb_share`). Anything wider
(team, org, or org-wide visibility) needs an approver; approvers may share directly into scopes they approve.

**Who approves:** admins approve everything in their org. A non-admin member can be named approver of specific
scopes (`hive user add a@x.io --approves team:t1`, or `hive token create ... --approves project:p1`); they see and
decide only requests into those scopes, and org-level requests stay with admins. Viewers never approve. Nobody
approves their own request.

| Tool | Who | What |
|------|-----|------|
| `kb_request_promotion(id, to, owner, org_wide, reason)` | author/editor of the item, member of the target | creates a pending request |
| `kb_list_promotions(status, mine)` | approvers: whole org queue with item content; others: own requests | review queue |
| `kb_decide_promotion(id, decision, note)` | approvers: approve/reject; requester: withdraw | decides |

Guarantees (all covered by tests on every backend):
- **Reviewed version only.** A request records the item revision. If the item is edited before
  approval, the request becomes `stale` and nothing is published.
- **Exactly one decision.** Decisions are compare-and-set transitions; concurrent approvers cannot both win.
- **No self-approval**, even if the requester is later made admin. One pending request per item.
- Approval moves the item and updates the search indexes' access rules in the same call; every step is audited
  (`promotion.request`, `promotion.approved|rejected|withdrawn|stale`).
- Approvers can read the content of any open request in their org, including private items. That is the
  review; it is audited.

**Crash safety:** before an approval is recorded, a repair job is queued; if the process dies before the item is
moved, the job finishes the move (or marks the request `stale` if the item changed meanwhile).

## Notifications
Optional, off by default. Each channel is a durable job per event (retried with backoff, a broken channel never
blocks the others or the workflow):
- `HIVE_NOTIFY_WEBHOOK_URL`: Slack-style incoming webhook (or any endpoint accepting JSON with a `text` field);
  gets every request and decision. Treat the URL as a secret.
- Email: `HIVE_SMTP_HOST`, `HIVE_SMTP_PORT` (587), `HIVE_SMTP_FROM`, optional `HIVE_SMTP_USER`/`HIVE_SMTP_PASSWORD`,
  `HIVE_SMTP_STARTTLS` (true). Requests go to the approvers of the target scope found in the user directory
  (admins + named approvers, not the requester, not disabled users); decisions go to the requester when their
  id is an email address (OAuth sign-in users).
Messages contain the item title, who asked, where to, the reason and the outcome, never the item body.
Delivery is at-least-once.


## Notification channels

Promotion requests and decisions go out through every channel that is configured: the chat webhook, email, and (new)
**direct messages in Slack and Microsoft Teams**. The two chat channels always exist and look at the stored integration
settings when they send, so switching Slack or Teams on or off on `/admin/integrations` changes delivery within seconds.
Slack needs the bot token (scopes `users:read.email`, `chat:write`); a person is found by email. Teams messages go to the
personal chat the person already has with the bot (remembered the last time they wrote to it); people who never did are
skipped, and email still reaches them. Only titles and who asked are sent, never item content. A chat service that is
down raises an error so the job runner retries; an integration that is off, or a person it cannot find, is skipped quietly.
