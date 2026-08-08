"""Every failed clip must explain itself in full, inline (Hugo 2026-08-03).

The samples below are VERBATIM stored errors from Hugo's own failed clips —
the point of the feature is that clips which failed BEFORE it shipped explain
themselves too, so the parser is tested against what is actually on disk.
"""

from __future__ import annotations

from character_swap import clip_failure

FAL_CONTENT_POLICY = (
    "[{'loc': ['body', 'prompt'], 'msg': 'The content could not be processed "
    "because it contained material flagged by a content checker.', 'type': "
    "'content_policy_violation', 'url': "
    "'https://docs.fal.ai/errors#content_policy_violation', 'input': "
    "{'prompt': 'He says enthusiastically to the camera in neutral Latin "
    "American Spanish: \"Vierte aceite de oliva sobre el ajo.\" Not NSFW.', "
    "'aspect_ratio': '9:16', 'duration': '8s', 'negative_prompt': 'subtitles, "
    "captions', 'resolution': '1080p', 'generate_audio': True, 'seed': None, "
    "'auto_fix': False, 'safety_tolerance': '4', 'image_url': "
    "'https://v3b.fal.media/files/b/0aa4de00/x_variant_v_927643.png'}}]"
)

FAL_NO_MEDIA = (
    "[{'loc': ['body'], 'msg': 'The model did not generate the expected output "
    "for this prompt. This may occur for several reasons, including unsafe "
    "content, a prompt that is incompatible with the selected media type.', "
    "'type': 'no_media_generated', 'input': {'prompt': 'She says hello.', "
    "'duration': '6s', 'resolution': '1080p'}}]"
)

GROK_MODERATION = (
    'Status failed (400): {"code":"Client specified an invalid argument",'
    '"error":"Generated video rejected by content moderation.",'
    '"usage":{"cost_in_usd_ticks":7020000000}}'
)

FAL_BALANCE = ("submit: fal fal-ai/kling-video/v3/standard/image-to-video "
               "submit failed: User is locked. Reason: Exhausted balance. Top "
               "up your balance at fal.ai/dashboard/billing.")

KLING_TIMEOUT = "fal Kling v3 job 019e807b-a0cf-7fa2-83b4-88b58178029b timed out after 600s"

DOWNSTREAM = ("[{'loc': ['body'], 'msg': 'Downstream service error', 'type': "
              "'downstream_service_error', 'input': {'prompt': 'A man talks.'}}]")


def test_no_error_means_no_explanation():
    assert clip_failure.explain(None) is None
    assert clip_failure.explain("") is None
    assert clip_failure.explain("   ") is None


def test_content_policy_is_classified_and_fully_unpacked():
    d = clip_failure.explain(FAL_CONTENT_POLICY, model="veo-3.1-fast")
    assert d["kind"] == "content_policy"
    assert d["provider_code"] == "content_policy_violation"
    assert "content checker" in d["provider_message"]
    assert d["doc_url"].startswith("https://docs.fal.ai/")
    assert d["field"] == "body.prompt"
    # The exact prompt that was submitted is what Hugo needs to see.
    assert "Vierte aceite de oliva" in d["prompt"]
    facts = {f["label"]: f["value"] for f in d["facts"]}
    assert facts["Modell"] == "veo-3.1-fast"
    assert facts["Längd"] == "8s"
    assert facts["Upplösning"] == "1080p"
    assert facts["Format"] == "9:16"
    assert facts["Startbild"] == "x_variant_v_927643.png"  # filename, not the URL
    assert d["raw"] == FAL_CONTENT_POLICY


def test_content_policy_does_not_blame_the_prompt_alone():
    """Measured 2026-08-03: a blocked IMAGE returns the same error with the
    prompt field flagged, even for a neutral prompt. The text must not send
    Hugo off rewording a prompt that was never the problem."""
    d = clip_failure.explain(FAL_CONTENT_POLICY)
    assert "startbilden" in d["what"].lower()
    assert "prompt" in d["what"].lower()


def test_silent_refusal_is_its_own_kind():
    d = clip_failure.explain(FAL_NO_MEDIA, model="veo-3.1-fast")
    assert d["kind"] == "silent_refusal"
    assert d["provider_code"] == "no_media_generated"
    assert "bilden" in d["what"].lower()
    assert d["prompt"] == "She says hello."


def test_grok_json_moderation_error():
    d = clip_failure.explain(GROK_MODERATION, model="grok-imagine-1.5")
    assert d["kind"] == "content_policy"
    assert d["provider_message"] == "Generated video rejected by content moderation."


def test_balance_lock_names_the_real_fix():
    d = clip_failure.explain(FAL_BALANCE, model="kling-v3")
    assert d["kind"] == "billing_locked"
    assert "fal.ai/dashboard/billing" in d["fix"]
    facts = {f["label"]: f["value"] for f in d["facts"]}
    assert facts["Fas"] == "vid submit"


def test_timeout_and_downstream_are_separated():
    assert clip_failure.explain(KLING_TIMEOUT)["kind"] == "timeout"
    assert clip_failure.explain(DOWNSTREAM)["kind"] == "provider_error"


def test_runner_written_reasons_are_classified():
    assert clip_failure.explain(
        "fel språk efter 3 tagningar: klippet talar engelska")["kind"] == "wrong_language"
    assert clip_failure.explain(
        "talar-agenten misslyckades: timeout")["kind"] == "speaker_fix_failed"
    assert clip_failure.explain(
        "German localization failed: boom")["kind"] == "localization_failed"
    assert clip_failure.explain(
        "interrupted (server restart)")["kind"] == "interrupted"
    assert clip_failure.explain(
        "submit: approved variant missing on disk")["kind"] == "missing_input"
    assert clip_failure.explain(
        "submit: Client error '429 Too Many Requests' for url "
        "'https://api.x.ai/v1/videos/generations'")["kind"] == "rate_limit"


def test_an_unreadable_line_blames_the_prompt_not_the_api_key():
    """The 2026-08-09 refusal. `localization_failed` tells Hugo to check his
    OpenAI key and quota, which is exactly wrong here — nothing was ever sent
    to the translator, because the line could not be lifted out of the prompt.
    The rule must therefore win over the broader `localization failed` one,
    and the advice must name the character to change."""
    real = ("German localization failed: the prompt carries a spoken line the "
            "extractor cannot read, so it can be neither translated to German "
            "nor language-checked: “Put baking soda on oranges”")
    got = clip_failure.explain(real)
    assert got["kind"] == "unreadable_line"
    assert "OPENAI_API_KEY" not in got["fix"]
    assert '"' in got["fix"]                      # it shows a straight quote
    assert "citattecken" in got["fix"].lower()


def test_unknown_error_still_shows_the_text_verbatim():
    d = clip_failure.explain("KaboomError: something entirely new")
    assert d["kind"] == "unknown"
    assert "something entirely new" in d["provider_message"]
    assert d["raw"] == "KaboomError: something entirely new"


def test_language_redirect_and_fallback_are_named():
    d = clip_failure.explain(
        FAL_CONTENT_POLICY, model="veo-3.1-fast", picked_model="kling-v3")
    facts = {f["label"]: f["value"] for f in d["facts"]}
    assert "kling-v3" in facts["Du valde"] and "omdirigerad" in facts["Du valde"]

    d2 = clip_failure.explain(
        "content-policy: reservmodellen grok-imagine-1.5 nekades också "
        "(rendering): blocked",
        model="grok-imagine-1.5", fallback_model="grok-imagine-1.5")
    assert d2["kind"] == "content_policy"
    assert "grok-imagine-1.5" in d2["what"]


def test_truncated_payload_still_yields_the_prompt():
    """SQLite rows from older runs can hold a cut-off payload — the regex
    fallback must still pull the prompt and the message out of it."""
    truncated = FAL_CONTENT_POLICY[:400]
    d = clip_failure.explain(truncated)
    assert d["kind"] == "content_policy"
    assert "Vierte aceite" in (d["prompt"] or "")
