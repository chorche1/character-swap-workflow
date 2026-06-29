"""GET /api/reengineer (light list) must resolve each final-video URL + a
character-name map for EVERY run — so runs BEYOND the hydrated newest-8 still
render their final-video players + names instead of bare "ch_…" IDs with no
player. (Hugo 2026-06-29 regression: older done runs showed "Final videos"
followed by raw char IDs and nothing playable.)
"""
from __future__ import annotations

import asyncio

from character_swap import api
from character_swap import reengineer as reengineer_mod


class _Char:
    def __init__(self, name):
        self.name = name


class _Job:
    def __init__(self, characters):
        self.characters = characters  # {cid: _Char}


def test_list_resolves_final_url_and_char_names(monkeypatch):
    state = {
        "re_id": "re_test1",
        "status": "done",
        "from_images": True,
        "job_id": "j_test1",
        "character_ids": ["cA", "cB"],
        # Present on disk; the light list must DROP it to stay light.
        "scenes": [{"scene_id": "s1"}],
        "finals": {
            "cA": {"status": "done",
                   "final_path": "/out/reengineer/re_test1/final_cA.mp4",
                   "edit_id": "ed1"},
        },
        "repurposed": {
            "cB": {"status": "done",
                   "final_path": "/out/reengineer/re_test1/repurpose_cB.mp4"},
        },
    }
    monkeypatch.setattr(reengineer_mod, "list_states", lambda: [dict(state)])

    job = _Job({"cA": _Char("Wang"), "cB": _Char("Cooper")})

    class _S:
        def get_job(self, jid):
            return job if jid == "j_test1" else None
    monkeypatch.setattr(api, "store", lambda: _S())
    monkeypatch.setattr(api, "_file_url", lambda p: "/files/output/" + p.name)

    rows = asyncio.run(api.reengineer_list())
    assert len(rows) == 1
    row = rows[0]
    # Stays light: the heavy per-scene list is dropped, no full job embedded.
    assert "scenes" not in row
    assert "job" not in row
    # Finals AND repurposed get a playable URL (drives the <video> + download).
    assert row["finals"]["cA"]["final_url"] == "/files/output/final_cA.mp4"
    assert row["repurposed"]["cB"]["final_url"] == "/files/output/repurpose_cB.mp4"
    # Names resolved so the cards show "Wang"/"Cooper", not "cA"/"cB".
    assert row["char_names"] == {"cA": "Wang", "cB": "Cooper"}


def test_list_handles_missing_job_and_finals(monkeypatch):
    # A run with no job (e.g. job not found) must not crash and must still
    # resolve final URLs; char_names just comes back empty.
    state = {
        "re_id": "re_test2",
        "status": "done",
        "job_id": None,
        "character_ids": ["cA"],
        "finals": {"cA": {"status": "done", "final_path": "/o/final_cA.mp4"}},
    }
    monkeypatch.setattr(reengineer_mod, "list_states", lambda: [dict(state)])

    class _S:
        def get_job(self, jid):
            return None
    monkeypatch.setattr(api, "store", lambda: _S())
    monkeypatch.setattr(api, "_file_url", lambda p: "/files/output/" + p.name)

    rows = asyncio.run(api.reengineer_list())
    assert rows[0]["char_names"] == {}
    assert rows[0]["finals"]["cA"]["final_url"] == "/files/output/final_cA.mp4"
