import pytest

from hive_mcp.connectors.base import Document
from hive_mcp.connectors.spec import ConnectorSpec, parse_specs
from hive_mcp.connectors.sync import STATUS_NS, ConnectorSync
from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.item import Status
from hive_mcp.domain.principal import Principal, Role

SPEC = ConnectorSpec("wiki", "urls", "org1", "bot", "p1", ("source/wiki",), 60)
BOT = Principal("bot", "org1", Role.MEMBER, frozenset(), frozenset({"p1"}))


class Fake:
    """A source whose documents the test edits between runs."""

    def __init__(self):
        self.docs: dict[str, tuple[str, str | None]] = {}  # key -> (text, version)
        self.loads: list[str] = []
        self.fail_listing = False
        self.fail_load: set[str] = set()

    def documents(self):
        async def gen():
            for key, (text, version) in list(self.docs.items()):
                yield Document(key, key, self._loader(key, text), version)
            if self.fail_listing:
                raise ValidationError("source unreachable")

        return gen()

    def _loader(self, key, text):
        async def load():
            self.loads.append(key)
            if key in self.fail_load:
                raise ValidationError("download failed")
            return f"{key}.md", f"# {key}\n\n{text}".encode()

        return load


@pytest.fixture
def sync(stack):
    b = stack.backend
    return ConnectorSync(stack.ingest, stack.knowledge, b.kv, b.audit, b.clock)


async def titles(stack, status=Status.ACTIVE):
    from hive_mcp.domain.item import ItemQuery

    page = await stack.knowledge.browse(
        BOT, ItemQuery(statuses=frozenset({status}), include_referenced=False, limit=100)
    )
    return sorted(v.item.title.split(".md")[0] for v in page.items)


async def test_new_documents_are_imported_into_the_project_with_provenance(stack, sync):
    src = Fake()
    src.docs = {"a": ("alpha words here", "v1"), "b": ("beta words here", "v1")}
    report = await sync.run(SPEC, src)
    assert (report.created, report.updated, report.retired, report.failed) == (2, 0, 0, {})
    assert await titles(stack) == ["a", "b"]
    from hive_mcp.domain.item import ItemQuery

    page = await stack.knowledge.browse(BOT, ItemQuery(include_referenced=False, limit=10))
    item = page.items[0].item
    assert item.scope.owner == "p1" and item.author_id == "bot"
    assert {"source/wiki", "connector/wiki"} <= item.tags
    assert item.source.startswith("connector:wiki:")


async def test_an_unchanged_version_is_not_downloaded_again(stack, sync):
    src = Fake()
    src.docs = {"a": ("alpha", "v1")}
    await sync.run(SPEC, src)
    src.loads.clear()
    report = await sync.run(SPEC, src)
    assert (report.unchanged, report.created) == (1, 0) and src.loads == []


async def test_same_content_under_a_new_version_is_unchanged_without_new_items(stack, sync):
    src = Fake()
    src.docs = {"a": ("alpha", "v1")}
    await sync.run(SPEC, src)
    src.docs = {"a": ("alpha", "v2")}  # touched, not edited
    report = await sync.run(SPEC, src)
    assert report.unchanged == 1 and await titles(stack) == ["a"]
    assert (await sync.run(SPEC, src)).unchanged == 1 and src.loads.count("a") == 2


async def test_a_changed_document_replaces_its_old_items_which_are_archived(stack, sync):
    src = Fake()
    src.docs = {"a": ("first version of the text", "v1")}
    await sync.run(SPEC, src)
    src.docs = {"a": ("second version of the text", "v2")}
    report = await sync.run(SPEC, src)
    assert (report.updated, report.created) == (1, 0)
    assert await titles(stack) == ["a"]  # one active copy
    assert await titles(stack, Status.ARCHIVED) == ["a"]  # the old one is kept, hidden


async def test_documents_gone_from_the_source_are_retired(stack, sync):
    src = Fake()
    src.docs = {k: (f"text of {k}", "v1") for k in "abcde"}
    await sync.run(SPEC, src)
    del src.docs["e"]
    report = await sync.run(SPEC, src)
    assert report.retired == 1 and await titles(stack) == ["a", "b", "c", "d"]
    assert (await sync.run(SPEC, src)).retired == 0


async def test_a_failed_listing_or_download_never_retires_anything(stack, sync):
    src = Fake()
    src.docs = {"a": ("alpha", "v1"), "b": ("beta", "v1")}
    await sync.run(SPEC, src)
    src.docs = {"a": ("alpha", "v1")}  # b vanished, but the listing then fails
    src.fail_listing = True
    report = await sync.run(SPEC, src)
    assert "nothing retired" in report.aborted and await titles(stack) == ["a", "b"]
    src.fail_listing = False
    src.docs = {"a": ("alpha", "v1"), "c": ("gamma", "v1")}
    src.fail_load = {"c"}  # a document failing makes the run unsafe for retirement too
    report = await sync.run(SPEC, src)
    assert list(report.failed) == ["c"] and report.retired == 0
    assert await titles(stack) == ["a", "b"]


async def test_an_empty_listing_does_not_wipe_the_knowledge_base_unless_forced(stack, sync):
    src = Fake()
    src.docs = {k: (f"text of {k}", "v1") for k in "abcdef"}
    await sync.run(SPEC, src)
    src.docs = {}
    report = await sync.run(SPEC, src)
    assert "vanished at once" in report.aborted and len(await titles(stack)) == 6
    forced = await sync.run(SPEC, src, allow_mass_retire=True)
    assert forced.retired == 6 and await titles(stack) == []


async def test_each_run_is_recorded_for_status_and_audit(stack, sync):
    src = Fake()
    src.docs = {"a": ("alpha", "v1")}
    await sync.run(SPEC, src)
    last = await stack.backend.kv.get(STATUS_NS, "wiki")
    assert "1 new" in last["summary"] and last["at"]
    events = [r.event for r in await stack.backend.audit.list("org1")]
    assert any(e.action == "connector.sync" and e.details["connector"] == "wiki" for e in events)


def test_specs_are_validated_with_actionable_errors():
    ok = {"name": "wiki", "type": "urls", "urls": ["https://x/y"], "org": "o", "owner": "bot",
          "project": "p1"}  # fmt: skip
    (spec,) = parse_specs([{**ok, "tags": ["Source/Wiki"], "every_minutes": 30}])
    assert spec.tags == ("source/wiki",) and spec.every_minutes == 30
    for bad in (
        {**ok, "type": "ftp"},
        {**ok, "name": "Bad Name"},
        {**ok, "every_minutes": 1},
        {**ok, "urls": "https://x"},
        {**ok, "urls": None},
        {**ok, "surprise": 1},
        {k: v for k, v in ok.items() if k != "project"},
        {**ok, "type": "github"},  # urls option is not a github option, repo missing
        "nope",
    ):
        with pytest.raises(ValidationError):
            parse_specs([bad])
    with pytest.raises(ValidationError, match="used twice"):
        parse_specs([ok, ok])
    drive = {"name": "d", "type": "drive", "org": "o", "owner": "b", "project": "p"}
    with pytest.raises(ValidationError, match="folder"):
        parse_specs([drive])
