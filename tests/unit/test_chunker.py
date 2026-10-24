from hypothesis import given
from hypothesis import strategies as st

from hive_mcp.ingest.chunker import chunk_text


def test_empty_and_wordless_text_have_no_chunks():
    assert chunk_text("") == [] and chunk_text("   \n\n \t ") == []


def test_short_text_is_one_chunk_and_paragraphs_pack():
    assert chunk_text("one two three") == ["one two three"]
    text = "alpha beta\n\ngamma delta\n\nepsilon"
    assert chunk_text(text, max_words=4) == ["alpha beta\n\ngamma delta", "epsilon"]


def test_long_paragraph_is_split_with_overlap():
    words = [f"w{i}" for i in range(50)]
    chunks = chunk_text(" ".join(words), max_words=20, overlap=5)
    assert len(chunks) == 3 and chunks[0].split()[-5:] == chunks[1].split()[:5]
    assert set(" ".join(chunks).split()) == set(words)


def test_deterministic():
    text = "para one.\n\n" + "word " * 300 + "\n\nlast para"
    assert chunk_text(text) == chunk_text(text)


@given(st.text(max_size=3000), st.integers(2, 60), st.integers(0, 30))
def test_every_word_is_covered_and_no_chunk_is_empty(text, max_words, overlap):
    chunks = chunk_text(text, max_words=max_words, overlap=min(overlap, max_words - 1))
    assert all(c.strip() for c in chunks)
    assert set(text.split()) <= set(" ".join(chunks).split())
    assert all(len(c.split()) <= max_words or "\n\n" not in c for c in chunks)
