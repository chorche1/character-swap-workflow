"""Defaults for the Swap + Reengineer outfit ("Kläder") control.

Hugo 2026-06-21: the swapped characters should wear THEIR OWN clothes by
default (not the scene/original-video outfit). The two entry endpoints
(reengineer_from_images = Swap tab, reengineer_create = Reengineer tab) must
therefore default outfit_mode to "character", mirroring background_source.
The UI mirrors this (app.js swapFromImages/reengineerGen outfitMode), but the
JS default isn't unit-testable here — these lock the API-layer defaults so a
silent revert is caught.
"""
from __future__ import annotations

import inspect

from fastapi import params

from character_swap import api, prompt_director


def _form_default(fn, name):
    p = inspect.signature(fn).parameters[name].default
    return getattr(p, "default", p) if isinstance(p, params.Form) else p


def test_outfit_mode_defaults_to_character():
    assert _form_default(api.reengineer_from_images, "outfit_mode") == "character"
    assert _form_default(api.reengineer_create, "outfit_mode") == "character"


def test_background_source_still_defaults_to_character():
    # Companion lock — outfit now matches the background_source precedent.
    assert _form_default(api.reengineer_from_images, "background_source") == "character"
    assert _form_default(api.reengineer_create, "background_source") == "character"


def test_character_outfit_directive_keeps_own_clothes():
    """The 'character' directive must take the clothing from the character
    reference (Image 2), not the scene — otherwise the new default would be a
    no-op vs 'scene'."""
    d = prompt_director._REENGINEER_OUTFIT_DIRECTIVES["character"]
    assert "own outfit" in d and "Image 2" in d
    assert d != prompt_director._REENGINEER_OUTFIT_DIRECTIVES["scene"]
