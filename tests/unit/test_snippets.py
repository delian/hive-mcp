from hive_mcp.search.snippets import best_chunk_index, snippet_for

CHUNKS = ["red green blue", "alpha beta gamma", "gamma delta epsilon"]


def test_picks_the_chunk_with_most_query_words():
    assert best_chunk_index(CHUNKS, "delta epsilon") == 2
    assert best_chunk_index(CHUNKS, "green") == 0


def test_ties_prefer_the_vector_hit_then_the_first_chunk():
    assert best_chunk_index(CHUNKS, "gamma") == 1  # first of the tied chunks
    assert best_chunk_index(CHUNKS, "gamma", preferred=2) == 2
    assert best_chunk_index(CHUNKS, "nothing matches", preferred=2) == 2
    assert best_chunk_index(CHUNKS, "nothing matches") == 0


def test_bad_inputs_are_safe():
    assert best_chunk_index([], "x") == 0
    assert best_chunk_index(CHUNKS, "x", preferred=99) == 0
    assert best_chunk_index(CHUNKS, "", preferred=-1) == 0


def test_snippet_is_truncated_and_never_empty():
    body = "word " * 500
    assert 0 < len(snippet_for(body, "word", limit=100)) <= 100
    assert snippet_for("???", "x") == "???"
