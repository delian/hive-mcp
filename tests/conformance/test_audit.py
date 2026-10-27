from hive_mcp.ports.audit import AuditEvent


def event(backend, org="org1", action="item.create"):
    return AuditEvent(org, "alice", action, backend.clock.now(), "item1", {"k": "v"})


async def test_append_only_ordered_and_org_scoped(backend):
    s1 = await backend.audit.append(event(backend))
    await backend.audit.append(event(backend, org="org2"))
    s3 = await backend.audit.append(event(backend, action="item.update"))
    assert s3 > s1
    rows = await backend.audit.list("org1")
    assert [r.event.action for r in rows] == ["item.create", "item.update"]
    assert all(r.event.org_id == "org1" for r in rows)


async def test_pagination_by_seq(backend):
    for _ in range(5):
        await backend.audit.append(event(backend))
    first = await backend.audit.list("org1", limit=2)
    rest = await backend.audit.list("org1", after_seq=first[-1].seq, limit=10)
    assert len(first) == 2 and len(rest) == 3
    assert [r.seq for r in first + rest] == sorted({r.seq for r in first + rest})


async def test_filters_by_actor_action_and_target(backend):
    def ev(actor, action, target, org="org1"):
        return AuditEvent(org, actor, action, backend.clock.now(), target, {})

    for e in [
        ev("alice", "item.create", "i1"),
        ev("bob", "item.create", "i2"),
        ev("alice", "item.update", "i1"),
        ev("alice", "item.create", "i3", org="org2"),
    ]:
        await backend.audit.append(e)
    acts = lambda rows: [(r.event.actor_id, r.event.action, r.event.target_id) for r in rows]  # noqa: E731
    assert acts(await backend.audit.list("org1", actor_id="alice")) == [
        ("alice", "item.create", "i1"),
        ("alice", "item.update", "i1"),
    ]
    assert acts(await backend.audit.list("org1", action="item.create")) == [
        ("alice", "item.create", "i1"),
        ("bob", "item.create", "i2"),
    ]
    assert acts(await backend.audit.list("org1", target_id="i1", action="item.update")) == [
        ("alice", "item.update", "i1")
    ]
    assert await backend.audit.list("org1", actor_id="nobody") == []
    assert len(await backend.audit.list("org1", actor_id="alice", limit=1)) == 1


async def test_newest_first_and_paging_backwards(backend):
    seqs = [await backend.audit.append(event(backend)) for _ in range(7)]
    await backend.audit.append(event(backend, org="org2"))
    newest = await backend.audit.list("org1", limit=3, newest_first=True)
    assert [r.seq for r in newest] == seqs[::-1][:3]
    older = await backend.audit.list("org1", limit=3, before_seq=newest[-1].seq)
    assert [r.seq for r in older] == seqs[::-1][3:6]  # before_seq pages backwards on its own
    last = await backend.audit.list("org1", limit=3, before_seq=older[-1].seq)
    assert [r.seq for r in last] == seqs[:1]
    assert await backend.audit.list("org1", before_seq=seqs[0]) == []
    window = await backend.audit.list("org1", after_seq=seqs[1], before_seq=seqs[5])
    assert [r.seq for r in window] == [seqs[4], seqs[3], seqs[2]]
    assert [r.seq for r in await backend.audit.list("org1", limit=2)] == seqs[
        :2
    ]  # default unchanged


async def test_filters_combine_with_newest_first(backend):
    for action in ("a", "b", "a", "b", "a"):
        await backend.audit.append(event(backend, action=action))
    rows = await backend.audit.list("org1", action="a", newest_first=True, limit=2)
    assert [r.event.action for r in rows] == ["a", "a"] and rows[0].seq > rows[1].seq


async def test_pseudonymise_replaces_one_person_in_one_org_and_keeps_the_events(backend):
    await backend.audit.append(event(backend))  # alice as actor, target item1
    await backend.audit.append(
        AuditEvent("org1", "root", "user.disable", backend.clock.now(), "alice")
    )
    await backend.audit.append(event(backend, org="org2"))  # another tenant's alice
    await backend.audit.append(AuditEvent("org1", "bob", "item.create", backend.clock.now(), "x"))
    changed = await backend.audit.pseudonymise("org1", "alice", "erased-1")
    assert changed == 2  # one as actor, one as target
    rows = await backend.audit.list("org1")
    assert [(r.event.actor_id, r.event.target_id) for r in rows] == [
        ("erased-1", "item1"),
        ("root", "erased-1"),
        ("bob", "x"),
    ]
    assert [r.event.actor_id for r in await backend.audit.list("org2")] == ["alice"]
    assert await backend.audit.pseudonymise("org1", "alice", "erased-2") == 0  # nothing left
