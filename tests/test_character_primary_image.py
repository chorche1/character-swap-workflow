"""Change a character's primary/preset reference image (Hugo 2026-06-28).

A library character is 1-to-many with images and carries a `primary_image_id`
(the ★ in the library, the default swap source). The library only ever DISPLAYED
the star — there was no way to re-point it. PATCH /api/characters/{id} now accepts
`primary_image_id`; these tests lock the round-trip, the lockstep `filename`
rewrite (so the legacy `url`/`filename` thumbnail follows), validation, and
serialization.
"""
from __future__ import annotations

import asyncio

import pytest

from character_swap import api
from character_swap.models import CharacterAsset, CharacterImage
from character_swap.state import SqliteStateStore, store


def _make(char_id: str) -> CharacterAsset:
    """A character with two images; image a is primary."""
    ch = CharacterAsset(
        char_id=char_id,
        filename=f"{char_id}_a.png",
        name="Ching",
        images=[
            CharacterImage(image_id=f"{char_id}_a", filename=f"{char_id}_a.png"),
            CharacterImage(image_id=f"{char_id}_b", filename=f"{char_id}_b.png"),
        ],
        primary_image_id=f"{char_id}_a",
    )
    store().add_character(ch)
    return ch


def test_set_primary_image_repoints_star_and_filename():
    _make("cpi1")
    out = asyncio.run(api.rename_character(
        "cpi1", api.RenameCharacterBody(primary_image_id="cpi1_b")))
    asset = store().get_character("cpi1")
    # The star moved AND the legacy filename (library thumbnail + default swap
    # source) follows it in lockstep.
    assert asset.primary_image_id == "cpi1_b"
    assert asset.filename == "cpi1_b.png"
    # Serialized dict reflects both, and url points at the new primary.
    assert out["primary_image_id"] == "cpi1_b"
    assert out["filename"] == "cpi1_b.png"
    assert out["url"].endswith("cpi1_b.png")


def test_set_primary_unknown_image_404s():
    _make("cpi2")
    with pytest.raises(api.HTTPException) as ei:
        asyncio.run(api.rename_character(
            "cpi2", api.RenameCharacterBody(primary_image_id="not_an_image")))
    assert ei.value.status_code == 404
    # The original primary is untouched on the rejected PATCH.
    assert store().get_character("cpi2").primary_image_id == "cpi2_a"


def test_set_primary_is_independent_of_other_fields():
    _make("cpi3")
    # A name-only PATCH must NOT disturb the primary, and a primary-only PATCH
    # must NOT disturb the name / voice / language.
    asyncio.run(api.rename_character("cpi3", api.RenameCharacterBody(name="Ching2")))
    assert store().get_character("cpi3").primary_image_id == "cpi3_a"
    asyncio.run(api.rename_character(
        "cpi3", api.RenameCharacterBody(primary_image_id="cpi3_b")))
    asset = store().get_character("cpi3")
    assert asset.name == "Ching2"
    assert asset.primary_image_id == "cpi3_b"


def test_set_primary_sqlite_round_trip(tmp_path):
    """The new primary must survive a server restart under the SQLite backend."""
    db = tmp_path / "state.sqlite3"
    s1 = SqliteStateStore(db_path=db)
    s1.add_character(CharacterAsset(
        char_id="cpis", filename="a.png", name="M",
        images=[CharacterImage(image_id="a", filename="a.png"),
                CharacterImage(image_id="b", filename="b.png")],
        primary_image_id="a"))
    ch = s1.get_character("cpis")
    ch.primary_image_id = "b"
    ch.filename = "b.png"
    s1.update_character(ch)

    s2 = SqliteStateStore(db_path=db)
    reloaded = s2.get_character("cpis")
    assert reloaded.primary_image_id == "b"
    assert reloaded.filename == "b.png"
