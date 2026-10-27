import pytest

from hive_mcp.domain.errors import NotifyError
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.promotion import PromotionStatus
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.security.directory import DirectoryUser, UserDirectory
from hive_mcp.security.jsonfile import AtomicJsonFile
from hive_mcp.services.models import RememberRequest
from hive_mcp.services.notifications import NOTIFY_JOB, NotificationService
from hive_mcp.services.promotions import PromotionService
from tests.conformance.factory import ROOT

TEAM = Scope(ScopeLevel.TEAM, "t1")
REQUESTER = Principal("bia@acme.io", "org1", Role.MEMBER, teams=frozenset({"t1"}))


class Recorder:
    def __init__(self, fail=0):
        self.sent, self.fail = [], fail

    async def send(self, notification):
        if self.fail:
            self.fail -= 1
            raise NotifyError("down")
        self.sent.append(notification)


@pytest.fixture
def wired(stack, tmp_path):
    directory = UserDirectory(AtomicJsonFile(tmp_path / "users.json", default=[]))
    for email, role, approves in [
        ("root@acme.io", Role.ADMIN, frozenset()),
        ("lead@acme.io", Role.MEMBER, frozenset({"team:t1"})),
        ("lead2@acme.io", Role.MEMBER, frozenset({"team:t2"})),
        ("off@acme.io", Role.ADMIN, frozenset()),
        (
            "bia@acme.io",
            Role.ADMIN,
            frozenset(),
        ),  # the requester is never told to approve their own
        ("other@elsewhere", Role.ADMIN, frozenset()),
    ]:
        org = "org2" if email.startswith("other") else "org1"
        directory.upsert(
            DirectoryUser(email, org, role, approves=approves, disabled=email.startswith("off"))
        )
    hook, mail = Recorder(), Recorder()
    notifications = NotificationService(
        channels={"webhook": hook, "email": mail},
        promotions=stack.backend.promotions,
        items=stack.backend.items,
        directory=directory,
        jobs=stack.backend.jobs,
    )
    promotions = PromotionService(
        items=stack.backend.items,
        promotions=stack.backend.promotions,
        indexing=stack.indexing,
        jobs=stack.backend.jobs,
        audit=stack.backend.audit,
        clock=stack.backend.clock,
        notifications=notifications,
    )

    async def run_jobs():
        done = 0
        while job := await stack.backend.jobs.claim("w", frozenset({NOTIFY_JOB}), 30):
            try:
                await notifications.handle(job)
            except NotifyError as exc:
                await stack.backend.jobs.fail(job.id, "w", str(exc), 1800)
            else:
                await stack.backend.jobs.complete(job.id, "w")
            done += 1
        return done

    return promotions, hook, mail, run_jobs


async def new_request(stack, promotions):
    item = (
        await stack.knowledge.remember(
            REQUESTER, RememberRequest(title="Pulse timing", body="SECRET BODY TEXT radar echo")
        )
    ).item
    return await promotions.request(
        REQUESTER, item.id, TEAM, Visibility.SCOPE, "useful for the team"
    )


async def test_a_request_tells_the_webhook_and_emails_only_the_right_approvers(stack, wired):
    promotions, hook, mail, run_jobs = wired
    await new_request(stack, promotions)
    assert await run_jobs() == 2  # one job per channel
    (chat,) = hook.sent
    assert chat.event == "promotion.requested" and "Pulse timing" in chat.subject
    assert (
        "bia@acme.io" in chat.text and "team:t1" in chat.text and "useful for the team" in chat.text
    )
    assert "SECRET BODY TEXT" not in chat.text + chat.subject  # content never leaves the server
    (email,) = mail.sent
    assert set(email.recipients) == {
        "root@acme.io",
        "lead@acme.io",
    }  # not t2's lead, not disabled/other org/requester


async def test_decisions_reach_the_requester_with_the_outcome(stack, wired):
    promotions, hook, mail, run_jobs = wired
    promo = await new_request(stack, promotions)
    await run_jobs()
    hook.sent.clear(), mail.sent.clear()
    await promotions.decide(ROOT, promo.id, "approve", "thanks")
    await run_jobs()
    assert mail.sent[0].recipients == ("bia@acme.io",)
    assert "approved" in mail.sent[0].subject and "thanks" in mail.sent[0].text
    assert "approved" in hook.sent[0].text


async def test_rejection_and_withdrawal_announce_correctly(stack, wired):
    promotions, _, mail, run_jobs = wired
    promo = await new_request(stack, promotions)
    await run_jobs()
    mail.sent.clear()
    await promotions.decide(ROOT, promo.id, "reject", "too vague")
    await run_jobs()
    assert "rejected" in mail.sent[0].subject and "too vague" in mail.sent[0].text
    again = await new_request2(stack, promotions)
    await run_jobs()
    mail.sent.clear()
    await promotions.decide(REQUESTER, again.id, "withdraw")
    assert await run_jobs() == 0  # a withdrawal needs no announcement


async def new_request2(stack, promotions):
    item = (
        await stack.knowledge.remember(REQUESTER, RememberRequest(title="Second", body="more text"))
    ).item
    return await promotions.request(REQUESTER, item.id, TEAM, Visibility.SCOPE)


async def test_a_failing_channel_is_retried_alone(stack, wired):
    promotions, hook, mail, run_jobs = wired
    hook.fail = 1
    await new_request(stack, promotions)
    await run_jobs()
    assert len(mail.sent) == 1 and not hook.sent  # email went out, webhook failed
    stack.backend.clock.advance(seconds=3600)
    await run_jobs()
    assert len(hook.sent) == 1 and len(mail.sent) == 1  # only the webhook was retried


async def test_notification_problems_never_break_the_workflow(stack, wired, monkeypatch):
    promotions, _, _, _ = wired
    from hive_mcp.domain.errors import StorageBusy

    async def boom(*a, **k):
        raise StorageBusy("queue down")

    monkeypatch.setattr(stack.backend.jobs, "enqueue", boom)
    promo = await new_request(stack, promotions)  # still created
    assert promo.status is PromotionStatus.PENDING


async def test_a_removed_channel_or_request_is_ignored(stack, wired):
    promotions, hook, _, run_jobs = wired
    await new_request(stack, promotions)
    await stack.backend.jobs.enqueue(
        "org1", NOTIFY_JOB, {"channel": "gone", "event": "promotion.requested", "promotion_id": "x"}
    )
    await stack.backend.jobs.enqueue(
        "org1",
        NOTIFY_JOB,
        {"channel": "webhook", "event": "promotion.requested", "promotion_id": "missing"},
    )
    await run_jobs()
    assert len(hook.sent) == 1
