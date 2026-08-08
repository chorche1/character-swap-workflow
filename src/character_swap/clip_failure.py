"""Explain WHY a clip failed, in full, next to the clip itself.

A failed clip used to carry one opaque string that the UI only showed as a
`title` tooltip — invisible on a phone, and unreadable even on desktop when the
payload is fal's 1500-character error dict (Hugo 2026-08-03: "jag vill se så
detaljerat som möjligt varför ett klipp failar, vid den failade genereringen").

`explain()` turns that stored string into a structured explanation:

* `kind`      — a stable slug the UI styles on
* `headline`  — the classified cause in Swedish, one line
* `what`/`fix`— what it means and what actually helps
* `provider_*`— the provider's own message/code/doc link, verbatim
* `facts`     — model, length, phase, the flagged field, the source image
* `prompt`    — the exact prompt the provider was given
* `raw`       — the untouched stored error, always available

It is a PURE parser over the text already persisted on `VideoVariant.error`, so
every clip that failed before this existed gets its explanation the moment the
page reloads — no migration, no re-render.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

__all__ = ["explain"]


# ── payload extraction ──────────────────────────────────────────────────────

def _parse_fal_payload(error: str) -> dict[str, Any] | None:
    """fal reports errors as a Python-repr list of validation dicts, sometimes
    behind a `submit: ` / `FalClientHTTPError: ` prefix. Return the first dict."""
    start = error.find("[{")
    if start < 0:
        return None
    blob = error[start:]
    for candidate in (blob, blob[: blob.rfind("}]") + 2] if "}]" in blob else blob):
        try:
            parsed = ast.literal_eval(candidate)
        except (ValueError, SyntaxError):
            continue
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            return parsed[0]
        if isinstance(parsed, dict):
            return parsed
    return None


def _parse_json_payload(error: str) -> dict[str, Any] | None:
    """Grok/xAI style: `Status failed (400): {"code": ..., "error": ...}`."""
    start = error.find("{")
    if start < 0:
        return None
    try:
        parsed = json.loads(error[start:])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _regex_field(error: str, key: str) -> str | None:
    """Last-resort single-field pull when neither parser could read the blob
    (a truncated payload still contains readable `'key': '...'` pairs)."""
    m = re.search(rf"['\"]{key}['\"]: ('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")", error)
    if not m:
        return None
    try:
        return str(ast.literal_eval(m.group(1)))
    except (ValueError, SyntaxError):
        return m.group(1).strip("'\"")


# ── classification ──────────────────────────────────────────────────────────

# (slug, headline, what it means, what actually helps)
_CONTENT_POLICY = (
    "content_policy",
    "⛔ Innehållskontrollen nekade klippet",
    "Leverantörens innehållsfilter stoppade jobbet innan något klipp "
    "renderades. Filtret tittar på BÅDE prompten och startbilden — fältet det "
    "pekar ut säger inte vilket av dem som fällde avgörandet (en helt neutral "
    "prompt på en spärrad bild ger samma fel med prompt-fältet utpekat).",
    "Kontrollen är inte deterministisk: samma prompt och bild kan passera vid "
    "ett nytt försök. Hjälper inte ↻ så skriv om repliken (hälsopåståenden på "
    "spanska nekas oftast) eller byt startbild — längd och upplösning ändrar "
    "ingenting.",
)
_SILENT_REFUSAL = (
    "silent_refusal",
    "⛔ Modellen vägrade tyst — inget klipp genererades",
    "Modellen returnerade ingen video utan att säga varför. Mätt 2026-08-03 "
    "sitter den här vägran nästan alltid på BILDEN (samma bild nekas om och om "
    "igen även med en helt neutral prompt), inte på texten.",
    "Testa ↻ en gång; håller den i sig är det bilden. Byt startbild — eller "
    "kör samma person i en annan miljö, spärren är ofta scen-/ljusberoende.",
)
_KINDS: list[tuple[re.Pattern[str], tuple[str, str, str, str]]] = [
    (re.compile(r"fel språk efter", re.I), (
        "wrong_language",
        "🗣 Klippet talade fel språk i varje tagning",
        "Karaktären är språkflaggad, men klippet transkriberades tillbaka till "
        "den ENGELSKA källrepliken efter alla omtagningar. Ett sådant klipp "
        "släpps aldrig in i en final.",
        "↻ kör om med den översatta prompten. Återkommer det: korta repliken "
        "eller flytta språkangivelsen närmare citatet.",
    )),
    (re.compile(r"talar-agenten misslyckades", re.I), (
        "speaker_fix_failed",
        "⚥ Talar-agenten misslyckades",
        "👥-agenten som skriver om vem i bilden som säger repliken kunde inte "
        "köras, och klippet stoppades hellre än att skickas med orden i fel "
        "persons mun.",
        "↻ kör om klippet. Håller felet i sig: kolla ANTHROPIC_API_KEY.",
    )),
    # MUST precede the localization_failed rule below — that one blames the
    # translator and sends Hugo to check his OpenAI quota, which is exactly
    # wrong here: nothing was ever sent to the translator, because the line
    # could not be lifted out of the prompt in the first place. The fix is in
    # the prompt, and it is one character (2026-08-09).
    (re.compile(r"extractor cannot read", re.I), (
        "unreadable_line",
        "🗣 Repliken går inte att läsa ut ur prompten",
        "Prompten beordrar tal, men citattecknen runt repliken går inte att "
        "tolka — därför kunde repliken varken översättas till karaktärens "
        "språk eller språkkontrolleras. Klippet stoppades innan någon kredit "
        "spenderades.",
        "Öppna scenens rörelseprompt och skriv repliken med RAKA dubbla "
        'citattecken: "så här" — inte ”så här”, \'så här\' eller ett '
        "citattecken som aldrig stängs. Kör sedan ↻ igen.",
    )),
    (re.compile(r"localization failed", re.I), (
        "localization_failed",
        "🗣 Översättningen av repliken misslyckades",
        "Repliken kunde inte översättas till karaktärens språk, och klippet "
        "stoppades hellre än att spelas in på engelska.",
        "↻ kör om klippet. Håller felet i sig: kolla OPENAI_API_KEY och kvoten.",
    )),
    # MUST precede the billing_locked rule below — this message quotes fal's
    # own "User is locked" text, so the broader pattern would swallow it and
    # send Hugo to a billing page that already shows money (2026-08-06).
    (re.compile(r"even though the balance is", re.I), (
        "billing_lock_lingering",
        "💳 fal:s lås ligger kvar trots saldo",
        "Kontot har pengar, men fal:s låsflagga från det tomma saldot sitter "
        "kvar en stund efter påfyllningen och släpper ojämnt — en del klipp "
        "tas emot, andra nekas.",
        "↻ ta om de failade klippen om någon minut. Fler går igenom för varje "
        "runda tills låset släppt helt.",
    )),
    (re.compile(r"Exhausted balance|User is locked|fal submits paused", re.I), (
        "billing_locked",
        "💳 fal-kontot är låst — saldot är slut",
        "fal vägrar ta emot jobb tills saldot fyllts på. Redan köade klipp kan "
        "bli hängande i kön under tiden.",
        "Fyll på på fal.ai/dashboard/billing och ↻ de failade klippen.",
    )),
    (re.compile(r"timed out after|timeout|timed out", re.I), (
        "timeout",
        "⏱ Tidsgränsen gick ut",
        "Leverantören hann inte bli klar inom klippets tidsgräns "
        "(VIDEO_TIMEOUT_SECS). Klippet kan mycket väl ha renderats färdigt på "
        "deras sida efteråt.",
        "↻ kör om klippet. Händer det på många klipp samtidigt är det kö hos "
        "leverantören, inte något fel i körningen.",
    )),
    (re.compile(r"rate.?limit|429|Too Many Requests", re.I), (
        "rate_limit",
        "🚦 Rate-limit hos leverantören",
        "För många anrop under kort tid — jobbet avvisades utan att renderas.",
        "Vänta en stund och ↻.",
    )),
    (re.compile(r"downstream_service_error|Downstream service error", re.I), (
        "provider_error",
        "⚠ Fel hos leverantören (downstream)",
        "fal nådde inte fram till modellen bakom endpointen. Det är ett "
        "driftfel på deras sida, inte något med prompten eller bilden.",
        "↻ kör om klippet — brukar funka direkt.",
    )),
    (re.compile(r"SSL|SSLError|ConnectionError|Connection reset|Read timed out"
                r"|Temporary failure in name resolution", re.I), (
        "network",
        "🌐 Nätverksfel mot leverantören",
        "Anropet nådde aldrig fram (TLS-/anslutningsfel).",
        "↻ kör om klippet.",
    )),
    (re.compile(r"interrupted \(server restart\)|job cancelled", re.I), (
        "interrupted",
        "🔌 Avbruten av serveromstart",
        "Klippet var i luften när servern startades om, så resultatet gick "
        "förlorat.",
        "↻ kör om klippet.",
    )),
    (re.compile(r"approved variant missing on disk", re.I), (
        "missing_input",
        "📁 Startbilden saknas på disk",
        "Den godkända swap-bilden som klippet skulle animeras från gick inte "
        "att läsa.",
        "Generera om bilden i steget innan och animera sedan om scenen.",
    )),
    (re.compile(r"salvage re-poll failed", re.I), (
        "poll_failed",
        "🔁 Kunde inte hämta hem klippet",
        "Klippet skickades iväg men statusen gick inte att läsa av efteråt.",
        "↻ kör om klippet.",
    )),
]

_UNKNOWN = (
    "unknown",
    "✕ Klippet misslyckades",
    "Felet matchar ingen känd orsak — leverantörens råa meddelande står nedan.",
    "↻ kör om klippet; upprepas exakt samma text är det värt att felsöka.",
)


def _classify(error: str, code: str, message: str) -> tuple[str, str, str, str]:
    haystack = f"{code} {message} {error}"
    if re.search(r"content_policy_violation|content checker"
                 r"|rejected by content moderation|content[ -]policy"
                 r"|nsfw", haystack, re.I):
        return _CONTENT_POLICY
    if re.search(r"no_media_generated|did not generate the expected output",
                 haystack, re.I):
        return _SILENT_REFUSAL
    for pattern, kind in _KINDS:
        if pattern.search(haystack):
            return kind
    return _UNKNOWN


# ── public entry point ──────────────────────────────────────────────────────

def explain(error: str | None, *, model: str | None = None,
            picked_model: str | None = None,
            fallback_model: str | None = None) -> dict[str, Any] | None:
    """Structured explanation of one clip failure, or None if there's no error.

    `model` is the model the clip ACTUALLY ran on, `picked_model` what the user
    chose (they differ after a 🗣 language redirect), `fallback_model` the
    content-policy rescue model if one was tried.
    """
    if not error or not str(error).strip():
        return None
    error = str(error)

    payload = _parse_fal_payload(error) or {}
    inputs = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    inputs = inputs or {}
    if not payload:
        js = _parse_json_payload(error) or {}
        if js:
            payload = {"msg": js.get("error") or js.get("message"),
                       "type": js.get("code")}

    message = str(payload.get("msg") or "").strip()
    code = str(payload.get("type") or "").strip()
    doc_url = str(payload.get("url") or "").strip() or None

    loc = payload.get("loc")
    field = ".".join(str(p) for p in loc) if isinstance(loc, list) and loc else None

    prompt = inputs.get("prompt") or _regex_field(error, "prompt")

    kind, headline, what, fix = _classify(error, code, message)

    # The runner wraps a fallback attempt's error — say plainly that BOTH
    # models refused rather than reporting only the last one.
    if fallback_model and re.search(r"reservmodellen", error, re.I):
        if kind == "content_policy":
            what = (f"Både den valda modellen och reservmodellen "
                    f"{fallback_model} nekade klippet på innehållsgrund. " + what)
        else:
            what = (f"Klippet lades om på reservmodellen {fallback_model}, "
                    f"som i sin tur misslyckades. " + what)

    phase = "submit" if error.lstrip().lower().startswith("submit:") else "rendering"

    facts: list[dict[str, str]] = []
    if model:
        facts.append({"label": "Modell", "value": model})
    if picked_model and model and picked_model != model:
        facts.append({"label": "Du valde", "value": f"{picked_model} (omdirigerad)"})
    if fallback_model:
        facts.append({"label": "Reservmodell", "value": fallback_model})
    facts.append({"label": "Fas",
                  "value": "vid submit" if phase == "submit" else "under rendering"})
    for key, label in (("duration", "Längd"), ("resolution", "Upplösning"),
                       ("aspect_ratio", "Format")):
        val = inputs.get(key) or _regex_field(error, key)
        if val:
            facts.append({"label": label, "value": str(val)})
    if code:
        facts.append({"label": "Felkod", "value": code})
    if field:
        facts.append({"label": "Flaggat fält", "value": field})
    image_url = inputs.get("image_url") or _regex_field(error, "image_url")
    if image_url:
        # Filename only — the provider's upload URL is long, expires, and adds
        # nothing next to the clip; the full URL stays in `raw`.
        facts.append({"label": "Startbild",
                      "value": str(image_url).rstrip("/").split("/")[-1]})
    negative = inputs.get("negative_prompt") or _regex_field(error, "negative_prompt")

    if not message:
        # No structured payload: the stored string IS the message, minus the
        # `submit: ` prefix the runner adds.
        message = re.sub(r"^submit:\s*", "", error.strip())
        if len(message) > 400:
            message = message[:400] + "…"

    # The raw payload rides along on every poll of a run with failed clips —
    # cap it so a pathological provider dump can't bloat the response.
    raw = error if len(error) <= 4000 else error[:4000] + "… (avkortat)"

    return {
        "kind": kind,
        "headline": headline,
        "what": what,
        "fix": fix,
        "provider_message": message,
        "provider_code": code or None,
        "doc_url": doc_url,
        "field": field,
        "facts": facts,
        "prompt": prompt,
        "negative_prompt": negative,
        "raw": raw,
    }
