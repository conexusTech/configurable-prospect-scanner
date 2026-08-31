"""Why this lead scored what it scored — written after scoring, from the breakdown.

**The PO's words, and they set the whole shape:** *"we do not need to explain anything, we
just need to derive it on the information available or being displayed on UI… explain why
it is a high scored prospect backed up with the best info available. Do not over explain."*

🔴 **Why the existing `ai_analysis` reads as nonsense, and it is not the model's fault.**
It runs in `ai_judgment` **before the score exists** and is never handed it; its prompt says
*"Stay stage reasoning."* So it argues about timing next to a number it has never seen. The
fix is WHEN it runs and WHAT it is given — not whether a model is involved.

**Cost, measured on run 741b7b3b rather than estimated.** That run cost $58.08 across 223
calls and 3,183 grounded search queries. The judgment phase — the one that writes prose — is
**24 calls and 0 grounded requests**, the only phase with none. A second pass of that shape
adds low single-digit dollars and **zero grounded searches**, so it does not touch the shared
quota whose exhaustion has taken every environment down.

🔴 **The output is CHECKED, not trusted.** A fabricated detail beside a score is worse for
customer trust than dull prose: it is the one failure that makes the number itself look
invented. `validate_explanation` rejects any number that is not in the inputs.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional, Sequence

#: Kept deliberately tight. "Do not over explain" is a ruling, not a preference, and a
#: model given room will fill it.
from aeo.phases._concurrent import DEFAULT_CALL_TIMEOUT_S, map_bounded

NEWLINE = chr(10)

MAX_CHARS = 420

_PROMPT = """\
You are writing one short paragraph for a salesperson looking at a list of prospects.

It answers exactly one question: WHY does this lead have this score?

WHAT YOU ARE GIVEN
Every fact below is already on the salesperson's screen. You have nothing else, and there
is nothing else to have.

{facts}

HOW TO WRITE IT
- Two or three sentences. Under {max_chars} characters. Shorter is better.
- Lead with what makes this lead worth calling, not with the arithmetic.
- Use the specific facts: the market, the signal and its date, the contact, the size.
- Quote or closely paraphrase the finding above when there is one — it is the most
  persuasive thing you have, because it is what we actually observed.
- Plain sentences. No bullet points, no headings, no markdown.

WHAT YOU MUST NOT DO
- Do not state any fact that is not above. No revenue, no employee counts you were not
  given, no industry claims, no dates other than the one given.
- Do not invent a reason the lead is good. If the facts are thin, say less.
- Do not restate the score as a sum. The salesperson can see the number.
- Do not give advice about what to do next.

Return ONLY the paragraph. No JSON, no quotes around it, no preamble.
"""


def build_facts(breakdown: dict[str, Any], lead: dict[str, Any]) -> list[str]:
    """The complete input set — two gate verdicts, four band values, the lane, and the
    scanner's own finding. **Nothing else, and that is what keeps invention
    unrepresentable rather than merely discouraged.**

    🔑 `signal_description` is load-bearing. The PO approved a written explanation partly
    because the samples quote the scanner's own finding verbatim; that quoted sentence is
    where the persuasion lives, and the template around it is scaffolding.
    """
    gates = breakdown.get("gates") or {}
    bands = breakdown.get("bands") or {}
    sig = breakdown.get("selected_signal") or {}
    facts: list[str] = []

    facts.append(
        f"- In the customer's target market: {'yes' if gates.get('target_market') else 'no'}"
        + (f" ({lead['state']})" if lead.get("state") else "")
    )
    facts.append(
        "- In an active buying window: "
        + ("yes" if gates.get("buying_window") else "no")
    )
    if sig.get("signal_date"):
        facts.append(f"- Signal date: {sig['signal_date']}")
    if sig.get("signal_description"):
        facts.append(f'- What we observed: "{sig["signal_description"]}"')
    elif sig.get("signal_type"):
        facts.append(f"- Signal type: {sig['signal_type']}")
    if lead.get("employee_count"):
        facts.append(f"- Employees: {lead['employee_count']}")
    contacts = [
        k.replace("contact_", "")
        for k in ("contact_name", "contact_email", "contact_phone")
        if str(lead.get(k) or "").strip()
    ]
    facts.append(
        f"- Contact on file: {', '.join(contacts)}" if contacts else "- Contact on file: none"
    )
    facts.append(f"- Score: {breakdown.get('total')} out of 100")
    if not gates.get("target_market") or not gates.get("buying_window"):
        missing = (
            "outside the target market"
            if not gates.get("target_market")
            else "no recent buying signal"
        )
        facts.append(f"- Why this is NOT a qualified lead: {missing}")
    # Band values last: useful to the model, but leading with them produces arithmetic.
    facts.append(
        "- Strength of the signal (0-8): {}; recency (0-4): {}; size (0-4): {}; "
        "contact (0-4): {}".format(
            bands.get("signal_strength", 0), bands.get("signal_recency", 0),
            bands.get("company_size", 0), bands.get("confirmed_contact", 0),
        )
    )
    return facts


_NUM = re.compile(r"\b\d[\d,]*\b")


def validate_explanation(
    text: str, breakdown: dict[str, Any], lead: dict[str, Any]
) -> Optional[str]:
    """``None`` if the paragraph is usable, else the reason to discard it.

    🔴 **Numbers are the check, because numbers are what a customer verifies.** A model
    that invents an employee count or a revenue figure produces a paragraph that reads
    beautifully and is false in the one way the reader can detect — and detecting it once
    costs the score its credibility permanently.

    Only DIGITS are policed. Adjectives cannot be validated and are not worth pretending
    to; the prompt constrains them and the fact list gives nothing to embellish.
    """
    if not text or not text.strip():
        return "empty"
    if len(text) > MAX_CHARS * 2:
        return f"too long ({len(text)} chars)"
    if any(m in text for m in ("- ", "**", "##", "\n\n")):
        return "contains markup or bullets"

    allowed: set[str] = set()
    for value in (
        breakdown.get("total"),
        lead.get("employee_count"),
        (breakdown.get("selected_signal") or {}).get("signal_date"),
        *(breakdown.get("bands") or {}).values(),
    ):
        for n in _NUM.findall(str(value or "")):
            allowed.add(n.replace(",", ""))
    allowed.add("100")  # the cap, always sayable
    desc = str((breakdown.get("selected_signal") or {}).get("signal_description") or "")
    for n in _NUM.findall(desc):
        allowed.add(n.replace(",", ""))

    for n in _NUM.findall(text):
        if n.replace(",", "") not in allowed:
            return f"states a number not in its inputs: {n}"
    return None


def explain_scores(
    prospects: Sequence[dict[str, Any]],
    *,
    provider: Callable[..., str],
    provider_config: dict[str, Any],
    emit: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, str]:
    """``{prospect_id: paragraph}`` for every prospect that produced a usable one.

    **A prospect whose explanation fails validation is ABSENT from the result**, never
    present with a fallback string. An absent explanation renders as no explanation, which
    is honest; a fabricated one is the defect this phase exists to prevent, and a silent
    fallback would make the two indistinguishable.
    """
    # 🔴 **Bounded, because `timeout_s` is NOT enforced by the provider.**
    #
    # `gemini_provider` carries `timeout_s: float,  # noqa: ARG001 — enforced by the
    # caller via signal/thread`. It accepts the argument and ignores it. A plain
    # sequential loop therefore passes a timeout nothing honours, and ONE hung call
    # blocks the phase forever — measured 2026-08-31 re-scoring MYgroup: 20+ minutes
    # of wall clock for 0.8s of CPU, no output, indistinguishable from slow work.
    #
    # `map_bounded` is what every other per-prospect phase uses and it enforces the
    # timeout with a thread, caps concurrency, and emits a liveness heartbeat. Reusing
    # it is also why this phase now behaves the same way under load as `ai_judgment`
    # rather than having its own failure mode.
    prepared: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for p in prospects:
        breakdown = ((p.get("score_factors") or {}).get("gated")) or {}
        if not breakdown:
            continue
        facts = build_facts(breakdown, p)
        prepared.append(
            (
                p,
                breakdown,
                _PROMPT.format(facts=NEWLINE.join(facts), max_chars=MAX_CHARS),
            )
        )

    def _one(item: tuple[dict[str, Any], dict[str, Any], str]) -> Optional[str]:
        _p, _breakdown, prompt = item
        # 🔴 `grounded=False` EXPLICITLY, and every kwarg named rather than splatted.
        # `gemini_provider` defaults `grounded=True`, so `provider(prompt,
        # **provider_config)` silently bought a Google Search on every prospect —
        # against this module's own docblock, which promises zero grounded requests.
        # That quota is shared with production and its exhaustion has already produced
        # a run that completed with 0 prospects and no error.
        #
        # Splatting also made the call depend on the caller assembling the exact kwarg
        # set: `_provider_config()` returns no `timeout_s`, so a standard config raised
        # TypeError per prospect — a config error wearing the costume of a data problem.
        return provider(
            prompt,
            model=provider_config.get("judgment_model") or provider_config.get("model"),
            temperature=provider_config.get("temperature", 0.1),
            retry_attempts=provider_config.get("retry_attempts", 3),
            timeout_s=provider_config.get("timeout_s", DEFAULT_CALL_TIMEOUT_S),
            grounded=False,
            phase="score_explanation",
        )

    raw_results = map_bounded(
        prepared,
        _one,
        max_concurrency=int(provider_config.get("max_concurrency", 2)),
        timeout_s=float(provider_config.get("timeout_s", DEFAULT_CALL_TIMEOUT_S)),
        on_error=lambda item, exc: emit(
            {"type": "score_explanation_failed",
             "prospect_id": item[0].get("id"), "error": str(exc)}
        ) if emit else None,
        label="explanations",
    )

    out: dict[str, str] = {}
    for (p, breakdown, _prompt), raw in zip(prepared, raw_results):
        if raw is None:
            continue  # failed or timed out; `on_error` already emitted
        text = str(raw or "").strip().strip('"')
        reason = validate_explanation(text, breakdown, p)
        if reason:
            if emit:
                emit({"type": "score_explanation_rejected",
                      "prospect_id": p.get("id"), "reason": reason})
            continue
        out[str(p.get("id"))] = text
    return out
