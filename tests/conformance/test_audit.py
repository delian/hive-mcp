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
