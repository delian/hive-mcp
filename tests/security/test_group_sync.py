"""Group sync: the Directory API client against a fake, the pure planner, and the safety rules."""

from __future__ import annotations

import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from hive_mcp.config import Settings
from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.principal import Role
from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.security.directory import DirectoryUser, UserDirectory
from hive_mcp.security.groups import factory
from hive_mcp.security.groups.client import GoogleGroups
from hive_mcp.security.groups.mapping import GroupMap, plan
from hive_mcp.security.groups.sync import sync_groups
from hive_mcp.security.jsonfile import AtomicJsonFile
from hive_mcp.security.oauth.upstream import UpstreamError

RAW = {
    "org_id": "acme",
    "groups": {
        "eng@acme.com": {"teams": ["eng"]},
        "apollo@acme.com": {"projects": ["apollo"]},
        "eng-leads@acme.com": {"approves": ["team:eng"], "role": "editor"},
        "admins@acme.com": {"role": "admin"},
    },
}
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
KEY_INFO = {"client_email": "svc@proj.iam.gserviceaccount.com", "private_key": PEM}


class FakeDirectoryApi:
    """Google's token endpoint and members.list, with paging, nesting flags and failures."""

    def __init__(self, groups: dict[str, list[dict]]) -> None:
        self.groups, self.token_requests, self.calls = groups, [], []
        self.fail: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            claims = jwt.decode(
                form["assertion"],
                KEY.public_key(),
                algorithms=["RS256"],
                audience="http://api/token",
            )
            self.token_requests.append(claims)
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer tok"
        group = request.url.path.split("/")[-2]
        self.calls.append(dict(request.url.params))
        if group in self.fail or group not in self.groups:
            return httpx.Response(404, json={})
        members = self.groups[group]
        start = int(request.url.params.get("pageToken", "0"))
        page = members[start : start + 2]  # tiny pages to exercise paging
        body: dict = {"members": page}
        if start + 2 < len(members):
            body["nextPageToken"] = str(start + 2)
        return httpx.Response(200, json=body)


def user(email: str, status: str = "ACTIVE", kind: str = "USER") -> dict:
    return {"email": email, "type": kind, "status": status}


def client_for(api: FakeDirectoryApi) -> GoogleGroups:
    http = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
    return GoogleGroups(
        KEY_INFO, "admin@acme.com", api_base="http://api", token_uri="http://api/token", http=http
    )


# ---------------------------------------------------------------- client
async def test_members_pages_filters_and_normalises():
    api = FakeDirectoryApi(
        {
            "eng@acme.com": [
                user("Ann@Acme.com"),
                user("bob@acme.com"),
                user("carl@acme.com", status="SUSPENDED"),
                user("nested@acme.com", kind="GROUP"),
                user("dee@acme.com"),
            ]
        }
    )
    got = await client_for(api).members("eng@acme.com")
    assert got == {"ann@acme.com", "bob@acme.com", "dee@acme.com"}
    assert len(api.calls) == 3 and all(c["includeDerivedMembership"] == "true" for c in api.calls)


async def test_the_service_account_assertion_impersonates_the_admin_with_the_group_scope():
    api = FakeDirectoryApi({"eng@acme.com": [user("a@acme.com")]})
    client = client_for(api)
    await client.members("eng@acme.com")
    await client.members("eng@acme.com")
    (claims,) = api.token_requests  # the token is reused
    assert claims["sub"] == "admin@acme.com" and claims["iss"] == KEY_INFO["client_email"]
    assert claims["scope"].endswith("admin.directory.group.member.readonly")
    assert claims["exp"] - claims["iat"] <= 3600 and claims["iat"] <= time.time()


async def test_unreadable_groups_and_bad_credentials_are_upstream_errors():
    api = FakeDirectoryApi({})
    with pytest.raises(UpstreamError, match=r"eng@acme\.com"):
        await client_for(api).members("eng@acme.com")
    with pytest.raises(UpstreamError, match="private_key"):
        GoogleGroups({"client_email": "x"}, "a@acme.com")
    denied = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(UpstreamError, match="refused"):
        await GoogleGroups(KEY_INFO, "a@acme.com", http=denied).members("g@acme.com")


# ---------------------------------------------------------------- planner
def mapping(**over) -> GroupMap:
    return GroupMap.parse({**RAW, **over}, ["acme.com"])


def members(**groups: set[str]) -> dict[str, set[str]]:
    return {f"{k.replace('_', '-')}@acme.com": v for k, v in groups.items()}


def test_memberships_follow_the_groups_and_only_the_mapped_values():
    manual = DirectoryUser(
        "ann@acme.com",
        "acme",
        Role.MEMBER,
        teams=frozenset({"eng", "legal"}),
        projects=frozenset({"apollo", "secret"}),
    )
    full = members(eng=set(), apollo=set(), eng_leads=set(), admins=set())
    (change,) = plan(mapping(), full, [manual])
    assert change.after.teams == {"legal"} and change.after.projects == {
        "secret"
    }  # mapped values removed, others kept
    both = members(eng={"ann@acme.com"}, apollo={"ann@acme.com"}, eng_leads=set(), admins=set())
    assert plan(mapping(), both, [manual]) == []  # already in step


def test_roles_come_from_the_highest_group_and_unlisted_managed_users_drop_to_default():
    ann = DirectoryUser("ann@acme.com", "acme", Role.ADMIN)
    bob = DirectoryUser("bob@acme.com", "acme", Role.MEMBER)
    full = members(
        eng={"ann@acme.com", "bob@acme.com"}, eng_leads={"bob@acme.com"}, admins=set(), apollo=set()
    )
    changes = {c.email: c.after for c in plan(mapping(), full, [ann, bob])}
    assert changes["ann@acme.com"].role is Role.MEMBER  # in a group, but no role group: default
    assert changes["bob@acme.com"].role is Role.EDITOR
    assert changes["bob@acme.com"].approves == {"team:eng"}
    top = members(eng=set(), eng_leads={"bob@acme.com"}, admins={"bob@acme.com"}, apollo=set())
    assert plan(mapping(), top, [bob])[0].after.role is Role.ADMIN


def test_users_outside_every_group_keep_their_role_and_only_lose_mapped_values():
    root = DirectoryUser("root@acme.com", "acme", Role.ADMIN, teams=frozenset({"eng", "ops"}))
    (change,) = plan(
        mapping(), members(eng=set(), apollo=set(), eng_leads=set(), admins=set()), [root]
    )
    assert change.after.role is Role.ADMIN and change.after.teams == {"ops"}


def test_new_users_are_created_only_when_enabled_and_in_an_allowed_domain():
    found = members(
        eng={"new@acme.com", "ext@other.com"}, apollo=set(), eng_leads=set(), admins=set()
    )
    assert plan(mapping(), found, []) == []
    created = plan(mapping(auto_create=True), found, [])
    assert [(c.email, c.action) for c in created] == [("new@acme.com", "created")]
    assert created[0].after.org_id == "acme" and created[0].after.teams == {"eng"}
    assert created[0].after.role is Role.MEMBER


def test_disable_missing_spares_admins_and_reenables_returning_users():
    mp = mapping(disable_missing=True)
    nobody = members(eng=set(), apollo=set(), eng_leads=set(), admins=set())
    ann = DirectoryUser("ann@acme.com", "acme", Role.MEMBER)
    root = DirectoryUser("root@acme.com", "acme", Role.ADMIN)
    changes = {c.email: c.action for c in plan(mp, nobody, [ann, root])}
    assert changes == {"ann@acme.com": "disabled"}
    back = DirectoryUser("ann@acme.com", "acme", Role.MEMBER, disabled=True)
    returned = plan(
        mp, members(eng={"ann@acme.com"}, apollo=set(), eng_leads=set(), admins=set()), [back]
    )
    assert returned[0].action == "enabled" and not returned[0].after.disabled
    assert plan(mapping(), nobody, [ann]) == []  # without the flag nobody is disabled


def test_invalid_maps_are_rejected():
    for bad in ({}, {"org_id": "a"}, {"org_id": "a", "groups": {"g@x.com": {"role": "boss"}}}):
        with pytest.raises(ValidationError):
            GroupMap.parse(bad)
    with pytest.raises(ValidationError):
        GroupMap.parse({"org_id": "a", "groups": {"g@x.com": {"approves": ["planet:mars"]}}})


# ---------------------------------------------------------------- sync
@pytest.fixture
def directory(tmp_path):
    return UserDirectory(AtomicJsonFile(tmp_path / "users.json", default=[]))


async def test_sync_writes_the_directory_and_principals_follow(directory):
    api = FakeDirectoryApi(
        {
            "eng@acme.com": [user("ann@acme.com"), user("bob@acme.com")],
            "apollo@acme.com": [user("ann@acme.com")],
            "eng-leads@acme.com": [user("bob@acme.com")],
            "admins@acme.com": [user("root@acme.com")],
        }
    )
    changes = await sync_groups(client_for(api), mapping(auto_create=True), directory)
    assert {c.email for c in changes} == {"ann@acme.com", "bob@acme.com", "root@acme.com"}
    bob = directory.lookup("bob@acme.com").principal()
    assert bob.role is Role.EDITOR and Scope(ScopeLevel.TEAM, "eng") in bob.approves
    assert directory.lookup("ann@acme.com").principal().projects == {"apollo"}
    assert directory.lookup("root@acme.com").principal().role is Role.ADMIN
    assert (
        await sync_groups(client_for(api), mapping(auto_create=True), directory) == []
    )  # idempotent


async def test_dry_run_writes_nothing(directory):
    api = FakeDirectoryApi({g: [user("ann@acme.com")] for g in RAW["groups"]})
    changes = await sync_groups(client_for(api), mapping(auto_create=True), directory, dry_run=True)
    assert changes and directory.list() == []


async def test_a_failing_or_empty_group_changes_nothing(directory):
    ann = DirectoryUser("ann@acme.com", "acme", Role.MEMBER, teams=frozenset({"eng"}))
    directory.upsert(ann)
    groups = {g: [user("ann@acme.com")] for g in RAW["groups"]}
    broken = FakeDirectoryApi({**groups, "apollo@acme.com": []})
    with pytest.raises(ValidationError, match="no members"):
        await sync_groups(client_for(broken), mapping(), directory)
    gone = FakeDirectoryApi({k: v for k, v in groups.items() if k != "apollo@acme.com"})
    with pytest.raises(UpstreamError):
        await sync_groups(client_for(gone), mapping(), directory)
    assert directory.lookup("ann@acme.com") == ann  # untouched
    await sync_groups(client_for(broken), mapping(), directory, allow_empty=True, dry_run=True)


async def test_the_configured_files_drive_a_sync(tmp_path, monkeypatch):
    (tmp_path / "map.json").write_text(json.dumps(RAW))
    (tmp_path / "key.json").write_text(json.dumps(KEY_INFO))
    settings = Settings(
        data_dir=tmp_path,
        group_map_file=tmp_path / "map.json",
        group_sync_key_file=tmp_path / "key.json",
        group_sync_admin="admin@acme.com",
        google_allowed_domains=["acme.com"],
    )
    api = FakeDirectoryApi({g: [user("ann@acme.com")] for g in RAW["groups"]})
    monkeypatch.setattr(factory, "GoogleGroups", lambda key, admin: client_for(api))
    changes = await factory.run_group_sync(settings, dry_run=True)
    assert (
        factory.group_sync_configured(settings) and changes == []
    )  # nobody listed in the directory yet
    with pytest.raises(ValidationError, match="needs"):
        await factory.run_group_sync(Settings(data_dir=tmp_path))
