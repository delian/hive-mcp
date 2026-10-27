import pytest

from hive_mcp.config import Settings
from hive_mcp.domain.errors import PermissionDenied, ValidationError
from hive_mcp.integrations.config import IntegrationConfig
from hive_mcp.integrations.secrets import SecretBox
from hive_mcp.integrations.store import NS, IntegrationStore, env_defaults
from tests.conformance.factory import ALICE, ROOT


def make(stack, settings=None, clock=None):
    b = stack.backend
    return IntegrationStore(
        lambda: b.kv, lambda: b.audit, SecretBox(b"k" * 32),
        env_defaults(settings or Settings()), b.clock, now=clock or (lambda: 0.0),
    )  # fmt: skip


def test_secrets_round_trip_and_are_bound_to_their_field():
    box = SecretBox(b"master-key-123456789012345678901234")
    sealed = box.seal("bot_token", "xoxb-secret")
    assert "xoxb" not in sealed and box.open("bot_token", sealed) == "xoxb-secret"
    assert box.seal("bot_token", "xoxb-secret") != sealed  # a fresh nonce every time
    with pytest.raises(ValidationError, match="signing_secret"):
        box.open("signing_secret", sealed)  # moved to another field
    with pytest.raises(ValidationError):
        SecretBox(b"another-key-12345678901234567890123").open("bot_token", sealed)
    with pytest.raises(ValidationError):
        box.open("bot_token", "not base64 at all!!")


async def test_the_environment_is_the_starting_point_and_stored_settings_win(stack):
    settings = Settings(
        slack_signing_secret="s" * 20, slack_team_id="T1", slack_users={"U1": "a@b.c"}
    )
    store = make(stack, settings)
    start = await store.get("slack")
    assert start.enabled and start.workspace == "T1" and start.secrets["signing_secret"] == "s" * 20
    assert not (await store.get("teams")).enabled
    changed = await store.update(ROOT, "slack", {"workspace": "T2", "allow_mentions": True})
    assert changed.workspace == "T2" and changed.secrets["signing_secret"] == "s" * 20  # kept
    stored = await stack.backend.kv.get(NS, "slack")
    assert "s" * 20 not in str(stored) and stored["sealed"]["signing_secret"]  # encrypted at rest
    fresh = make(stack, Settings())  # a new process without the environment variables
    again = await fresh.get("slack")
    assert again.workspace == "T2" and again.secrets["signing_secret"] == "s" * 20


async def test_validation_blocks_incomplete_or_bad_settings_and_stores_nothing(stack):
    store = make(stack)
    with pytest.raises(ValidationError, match="workspace"):
        await store.update(ROOT, "slack", {"enabled": True})
    with pytest.raises(ValidationError, match="signing secret"):
        await store.update(ROOT, "slack", {"enabled": True, "workspace": "T1"})
    with pytest.raises(ValidationError, match="bot token or a user map"):
        await store.update(
            ROOT, "slack", {"enabled": True, "workspace": "T1"}, {"signing_secret": "x" * 20}
        )
    with pytest.raises(ValidationError, match="app id and app password"):
        await store.update(ROOT, "teams", {"enabled": True, "workspace": "tenant-1"})
    for bad in (
        {"channel_projects": {"C1": "bad project!"}},
        {"users": {"U1": "no-at"}},
        {"workspace": "a b"},
    ):
        with pytest.raises(ValidationError):
            await store.update(ROOT, "slack", bad)
    with pytest.raises(ValidationError, match="unknown settings"):
        await store.update(ROOT, "slack", {"signing_secret": "oops"})
    with pytest.raises(ValidationError, match="unknown settings"):
        await store.update(ROOT, "slack", {}, {"app_password": "x"})  # a Teams secret on Slack
    assert await stack.backend.kv.get(NS, "slack") is None
    with pytest.raises(ValidationError):
        await store.get("discord")


async def test_only_admins_change_settings_and_every_change_is_audited_without_secrets(stack):
    store = make(stack)
    with pytest.raises(PermissionDenied):
        await store.update(ALICE, "slack", {"workspace": "T1"})
    await store.update(
        ROOT, "slack", {"enabled": True, "workspace": "T1"},
        {"signing_secret": "super-secret-value-123", "bot_token": "xoxb-very-secret"},
    )  # fmt: skip
    events = [r.event for r in await stack.backend.audit.list("org1")]
    update = next(e for e in events if e.action == "integration.update")
    assert update.actor_id == "root" and update.target_id == "slack"
    assert "signing_secret (secret)" in update.details["changed"]
    assert "super-secret" not in str(update) and "xoxb" not in str(update)
    cleared = await store.update(ROOT, "slack", {"enabled": False}, {"bot_token": ""})
    assert "bot_token" not in cleared.secrets and cleared.secrets["signing_secret"]
    assert cleared.public()["secrets_set"] == ["signing_secret"]


async def test_changes_are_visible_within_the_cache_window_only_after_it_or_at_once_for_the_writer(
    stack,
):
    t = [0.0]
    a, b = make(stack, clock=lambda: t[0]), make(stack, clock=lambda: t[0])
    await a.get("teams")
    await b.get("teams")  # both cached
    await a.update(ROOT, "teams", {"workspace": "tenant-9"})
    assert (await a.get("teams")).workspace == "tenant-9"  # the writer sees it at once
    assert (await b.get("teams")).workspace == ""  # another replica: after the cache window
    t[0] = 6.0
    assert (await b.get("teams")).workspace == "tenant-9"


def test_public_view_never_contains_secret_values():
    cfg = IntegrationConfig("slack", True, "T1", secrets={"signing_secret": "hunter2hunter2"})
    assert "hunter2" not in str(cfg.public()) and cfg.public()["secrets_set"] == ["signing_secret"]
    assert "secrets_set" not in cfg.to_record() and "secrets" not in cfg.to_record()


async def test_secrets_that_cannot_be_read_switch_the_integration_off_instead_of_failing(
    stack, caplog
):
    b = stack.backend
    first = IntegrationStore(
        lambda: b.kv, lambda: b.audit, SecretBox(b"k" * 32), env_defaults(Settings()), b.clock
    )
    await first.update(
        ROOT,
        "slack",
        {"enabled": True, "workspace": "T1"},
        {"signing_secret": "s" * 20, "bot_token": "xoxb-1"},
    )
    rotated = IntegrationStore(  # HIVE_SECRET_KEY was changed
        lambda: b.kv,
        lambda: b.audit,
        SecretBox(b"other-key" * 4),
        env_defaults(Settings()),
        b.clock,
    )
    config = await rotated.get("slack")
    assert not config.enabled and config.secrets == {} and config.workspace == "T1"
    assert "cannot be read" in caplog.text
    fixed = await rotated.update(
        ROOT, "slack", {"enabled": True}, {"signing_secret": "n" * 20, "bot_token": "xoxb-2"}
    )
    assert fixed.enabled and fixed.secrets["bot_token"] == "xoxb-2"  # the admin enters them again
