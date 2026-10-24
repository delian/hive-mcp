import numpy as np
import pytest

from hive_mcp.domain.errors import ValidationError
from hive_mcp.embeddings.hashing import HashingEmbedder

EMB = HashingEmbedder(128)


async def test_unit_norm_deterministic_and_overlap_is_closer():
    a, b, c = await EMB.embed(
        ["radar range equation", "radar range measurement", "chocolate cake recipe"]
    )
    assert np.linalg.norm(a) == pytest.approx(1.0) and a.shape == (128,)
    assert float(a @ b) > float(a @ c)
    assert np.array_equal(a, (await EMB.embed(["Radar RANGE equation"]))[0])  # case-insensitive


@pytest.mark.parametrize("bad", ["", "   ", "!!! ???"])
async def test_wordless_text_is_rejected(bad):
    with pytest.raises(ValidationError):
        await EMB.embed([bad])
