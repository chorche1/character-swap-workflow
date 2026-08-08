"""Reorder a character's reference images (Hugo 2026-08-08).

The library gallery's ORDER is now load-bearing: the Swap/Reengineer forms have
a "Använd bild N för alla" picker that resolves N by POSITION, so the user must
be able to decide what position 1/2/3 mean. PATCH /api/characters/{id} takes
`image_order`.

Two invariants these tests exist to hold:

* Order is a permutation — never an add, drop or dedupe. A partial list would
  silently delete the images it omitted, so it is refused loudly (Hugo's
  standing rule) with the character left exactly as it was.
* Order and the ★ primary are INDEPENDENT (Hugo's call). Reordering must not
  move the star, `filename`, or the resolved swap source — otherwise sorting
  the gallery would quietly re-point every future job.
"""
from __future__ import annotations

import asyncio

import pytest

from character_swap import api
from character_swap.models import CharacterAsset, CharacterImage
from character_swap.state import SqliteStateStore, store


def _make(char_id: str, n: int = 3, primary: int = 0) -> CharacterAsset:
    """A character with `n` images (a, b, c…); the `primary`-th is the ★."""
    letters = "abcdefg"[:n]
    ch = CharacterAsset(
        char_id=char_id,
        filename=f"{char_id}_{letters[primary]}.png",
        name="Ching",
        images=[
            CharacterImage(image_id=f"{char_id}_{L}", filename=f"{char_id}_{L}.png")
            for L in letters
        ],
        primary_image_id=f"{char_id}_{letters[primary]}",
    )
    store().add_character(ch)
    return ch


def _ids(char_id: str) -> list[str]:
    return [i.image_id for i in store().get_character(char_id).images]


def test_reorder_round_trip():
    _make("cio1")
    out = asyncio.run(api.rename_character(
        "cio1", api.RenameCharacterBody(
            image_order=["cio1_c", "cio1_a", "cio1_b"])))
    assert _ids("cio1") == ["cio1_c", "cio1_a", "cio1_b"]
    # The serialized dict is what the library renders and what the bulk picker
    # counts positions in — it must agree with the stored order.
    assert [i["image_id"] for i in out["images"]] == ["cio1_c", "cio1_a", "cio1_b"]


def test_reorder_leaves_the_star_and_the_swap_source_alone():
    """The whole point of Hugo's "★ förblir separat" answer: image c can sit
    first while image a is still the character's reference image."""
    _make("cio2")  # primary = cio2_a
    out = asyncio.run(api.rename_character(
        "cio2", api.RenameCharacterBody(
            image_order=["cio2_c", "cio2_b", "cio2_a"])))
    asset = store().get_character("cio2")
    assert asset.primary_image_id == "cio2_a"
    assert asset.filename == "cio2_a.png"
    assert out["primary_image_id"] == "cio2_a"
    assert out["url"].endswith("cio2_a.png")
    # And the resolver every job goes through still lands on the same file.
    assert asset.resolve_source_filename(None) == "cio2_a.png"


def test_partial_order_is_refused_and_changes_nothing():
    """A list missing an id would DROP that image. Refuse loudly instead."""
    _make("cio3")
    with pytest.raises(api.HTTPException) as ei:
        asyncio.run(api.rename_character(
            "cio3", api.RenameCharacterBody(image_order=["cio3_b", "cio3_a"])))
    assert ei.value.status_code == 400
    assert "cio3_c" in str(ei.value.detail)
    assert _ids("cio3") == ["cio3_a", "cio3_b", "cio3_c"]


def test_unknown_image_404s_and_changes_nothing():
    _make("cio4")
    with pytest.raises(api.HTTPException) as ei:
        asyncio.run(api.rename_character(
            "cio4", api.RenameCharacterBody(
                image_order=["cio4_a", "cio4_b", "nope"])))
    assert ei.value.status_code == 404
    assert _ids("cio4") == ["cio4_a", "cio4_b", "cio4_c"]


def test_duplicate_ids_are_refused():
    """Duplicates pass the same-length check but would clone one image and
    lose another."""
    _make("cio5")
    with pytest.raises(api.HTTPException) as ei:
        asyncio.run(api.rename_character(
            "cio5", api.RenameCharacterBody(
                image_order=["cio5_a", "cio5_a", "cio5_b"])))
    assert ei.value.status_code == 400
    assert _ids("cio5") == ["cio5_a", "cio5_b", "cio5_c"]


def test_reorder_is_independent_of_other_patch_fields():
    _make("cio6")
    asyncio.run(api.rename_character("cio6", api.RenameCharacterBody(name="Ching2")))
    assert _ids("cio6") == ["cio6_a", "cio6_b", "cio6_c"]
    asyncio.run(api.rename_character(
        "cio6", api.RenameCharacterBody(image_order=["cio6_b", "cio6_c", "cio6_a"])))
    asset = store().get_character("cio6")
    assert asset.name == "Ching2"
    assert [i.image_id for i in asset.images] == ["cio6_b", "cio6_c", "cio6_a"]


def test_reorder_pins_an_implicit_primary_before_it_can_drift():
    """A character with no explicit ★ resolves it as images[0] (_char_to_dict).
    Reordering such a character must not silently move the star — the handler
    pins the current one first."""
    ch = CharacterAsset(
        char_id="cio7", filename="cio7_a.png", name="M",
        images=[CharacterImage(image_id="cio7_a", filename="cio7_a.png"),
                CharacterImage(image_id="cio7_b", filename="cio7_b.png")],
        primary_image_id=None)
    store().add_character(ch)
    out = asyncio.run(api.rename_character(
        "cio7", api.RenameCharacterBody(image_order=["cio7_b", "cio7_a"])))
    assert store().get_character("cio7").primary_image_id == "cio7_a"
    assert out["url"].endswith("cio7_a.png")


def test_reorder_survives_restart_under_sqlite(tmp_path):
    """`character_images.position` is written from the list index and read back
    ORDER BY position — the reorder is worthless if it doesn't survive."""
    db = tmp_path / "state.sqlite3"
    s1 = SqliteStateStore(db_path=db)
    s1.add_character(CharacterAsset(
        char_id="cios", filename="a.png", name="M",
        images=[CharacterImage(image_id="a", filename="a.png"),
                CharacterImage(image_id="b", filename="b.png"),
                CharacterImage(image_id="c", filename="c.png")],
        primary_image_id="a"))
    ch = s1.get_character("cios")
    ch.images = [ch.images[2], ch.images[0], ch.images[1]]
    s1.update_character(ch)

    s2 = SqliteStateStore(db_path=db)
    reloaded = s2.get_character("cios")
    assert [i.image_id for i in reloaded.images] == ["c", "a", "b"]
    assert reloaded.primary_image_id == "a"
