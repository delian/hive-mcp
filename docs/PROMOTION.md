# Promotion (sharing beyond a project)

Items are private. A member may share to a project they belong to (`kb_share`). Anything wider
(team, org, or org-wide visibility) needs an approver (role `admin`); admins may share directly.

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

Not built yet: notifying approvers (they poll `kb_list_promotions`), per-team approvers (any org admin
approves), and a crash between claiming an approval and moving the item (would leave `approved` with the
item unmoved; the request can be re-filed).
