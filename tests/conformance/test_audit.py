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
