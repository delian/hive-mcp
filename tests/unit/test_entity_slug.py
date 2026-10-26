import pytest

from hive_mcp.domain.entity import MAX_SLUG, alias_tag, aliases_of, entity_slug
from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.tags import normalize_tag


@pytest.mark.parametrize(
    "variant",
    [
        "Pulse-Doppler  radar",
        "pulse doppler radar",
        "PULSE_DOPPLER RADAR",
        "  pulse.doppler/radar! ",
    ],
)
def test_spelling_variants_are_one_name(variant):
    assert entity_slug(variant) == "pulse-doppler-radar"


def test_symbols_keep_names_apart_and_other_scripts_work():
    assert len({entity_slug("C++"), entity_slug("C#"), entity_slug("C")}) == 3
    assert entity_slug("R&D") != entity_slug("RD")
    assert entity_slug("Größe") == "grösse"
    assert entity_slug("北京 大学") == "北京-大学"


def test_long_names_are_cut_to_a_valid_tag_and_empty_names_refused():
    slug = entity_slug("word " * 40)
    assert len(slug) <= MAX_SLUG and not slug.endswith("-")
    assert normalize_tag(alias_tag("word " * 40)) == alias_tag("word " * 40)  # always a valid tag
    for bad in ("", "   ", "!!!", "---"):
        with pytest.raises(ValidationError):
            entity_slug(bad)


def test_aliases_of_reads_only_alias_tags():
    assert aliases_of(frozenset({"alias/a", "alias/b-c", "entity/person", "alias"})) == {"a", "b-c"}
