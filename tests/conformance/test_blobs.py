import pytest

from hive_mcp.domain.errors import NotFound
from hive_mcp.ports.blobs import BlobRef


async def test_roundtrip_and_idempotent_put(backend):
    ref = await backend.blobs.put("org1", b"hello")
    assert await backend.blobs.put("org1", b"hello") == ref and ref.size == 5
    assert await backend.blobs.get(ref) == b"hello" and await backend.blobs.exists(ref)


async def test_blobs_are_namespaced_per_org(backend):
    ref1 = await backend.blobs.put("org1", b"same bytes")
    ref2 = await backend.blobs.put("org2", b"same bytes")
    assert ref1.sha256 == ref2.sha256 and ref1 != ref2
    await backend.blobs.delete(ref1)
    assert not await backend.blobs.exists(ref1) and await backend.blobs.exists(ref2)
    guessed = BlobRef("org3", ref2.sha256, ref2.size)  # knowing the hash must not grant access
    assert not await backend.blobs.exists(guessed)
    with pytest.raises(NotFound):
        await backend.blobs.get(guessed)


async def test_delete_is_idempotent(backend):
    ref = await backend.blobs.put("org1", b"x")
    await backend.blobs.delete(ref)
    await backend.blobs.delete(ref)
    with pytest.raises(NotFound):
        await backend.blobs.get(ref)
