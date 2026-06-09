"""Swap must never generate with a Google/Gemini image model — even for jobs
created BEFORE the Nano Banana models were removed from the Swap picker.

`runner._swap_image_model(job)` coerces any Gemini model to gpt-image; other
models pass through unchanged.
"""
from __future__ import annotations

import pytest

from character_swap import runner
from character_swap.models import Job


def _job(model: str) -> Job:
    return Job(job_id="j", title="t", scene_id="s", scene_image_path="/p.png",
               image_model=model)


@pytest.mark.parametrize("model", ["nano-banana", "nano-banana-pro"])
def test_gemini_models_coerced_to_gpt_image(model):
    assert runner._swap_image_model(_job(model)) == "gpt-image"


@pytest.mark.parametrize("model", ["gpt-image", "grok-image", "flux-pro", "dall-e-3"])
def test_non_gemini_models_pass_through(model):
    assert runner._swap_image_model(_job(model)) == model


def test_blank_model_defaults_to_gpt_image():
    assert runner._swap_image_model(_job("")) == "gpt-image"


def test_is_gemini_detection():
    assert runner._is_gemini_image_model("nano-banana-pro") is True
    assert runner._is_gemini_image_model("nano-banana") is True
    assert runner._is_gemini_image_model("gpt-image") is False
    assert runner._is_gemini_image_model("grok-image") is False
