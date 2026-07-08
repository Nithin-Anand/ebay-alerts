import json

import httpx
import structlog

from .models import Listing, LlmConfig, Verdict

log = structlog.get_logger()

_SYSTEM_PROMPT = """\
You are an eBay listing evaluator helping a UK buyer decide whether to bid.
Analyse the listing and return ONLY a valid JSON object — no markdown, no extra text.

Required JSON schema:
{
  "recommend": "bid" | "maybe" | "skip",
  "score": <integer 0-10>,
  "concerns": ["<issue>", ...],
  "notes": "<1-2 sentence summary>"
}

Scoring guide:
  9-10  Excellent condition, great value — strong buy signal
  7-8   Good condition, fair price — worth bidding
  4-6   Some concerns; review the listing before bidding
  1-3   Significant issues; probably not worth it
  0     Definite skip (broken, for parts, major defects clearly stated)

recommend field rules:
  "bid"   → score 7-10 and no dealbreakers
  "maybe" → score 4-6 or uncertainty about condition
  "skip"  → score 0-3 or a clear dealbreaker is present

concerns: list each specific issue you found (empty array if none).
notes: one or two sentences summarising your overall assessment."""


def _build_user_prompt(listing: Listing, criteria: str) -> str:
    lines = [
        f"Evaluation criteria: {criteria}",
        "",
        "Listing details:",
        f"  Title:          {listing.title}",
        f"  Price:          £{listing.display_price} {listing.currency}"
        f"{' (current bid)' if listing.is_auction else ''}",
        f"  Condition:      {listing.condition or 'Not specified'}",
        f"  Buying options: {', '.join(listing.buying_options) or 'Not specified'}",
    ]

    if listing.seller_username:
        seller = listing.seller_username
        if listing.seller_feedback_percentage:
            seller += f" ({listing.seller_feedback_percentage}% positive feedback)"
        lines.append(f"  Seller:         {seller}")

    if listing.short_description:
        lines.append(f"  Description:    {listing.short_description}")

    if listing.end_time:
        lines.append(f"  Ends:           {listing.end_time}")

    # Image URLs are included so vision models can use them;
    # text-only models will ignore this field.
    if listing.image_urls:
        lines.append(f"  Image URLs:     {', '.join(listing.image_urls[:5])}")

    lines.append("")
    lines.append("Return your JSON verdict now.")
    return "\n".join(lines)


class OllamaClient:
    def __init__(self, base_url: str, default_model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        # LLM calls can be slow — generous timeout
        self._http = httpx.AsyncClient(timeout=120)

    async def analyse(self, listing: Listing, config: LlmConfig) -> Verdict:
        model = config.model or self._default_model
        user_prompt = _build_user_prompt(listing, config.criteria)

        payload = {
            "model": model,
            "format": "json",  # Ollama JSON mode — constrains output to valid JSON
            "stream": False,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        }

        try:
            resp = await self._http.post(f"{self._base_url}/api/chat", json=payload)
            resp.raise_for_status()
            raw_content: str = resp.json()["message"]["content"]
        except Exception as exc:
            log.warning("ollama request failed", item_id=listing.item_id, error=str(exc))
            return Verdict(
                recommend="maybe",
                score=5,
                concerns=["LLM unavailable"],
                notes=f"Could not reach Ollama ({type(exc).__name__}): {exc}",
            )

        try:
            data = json.loads(raw_content)
            verdict = Verdict.model_validate(data)
        except Exception as exc:
            log.warning(
                "ollama response unparseable",
                item_id=listing.item_id,
                error=str(exc),
                raw=raw_content[:300],
            )
            return Verdict(
                recommend="maybe",
                score=5,
                concerns=["LLM output could not be parsed"],
                notes=raw_content[:400],
            )

        log.info(
            "llm verdict",
            item_id=listing.item_id,
            recommend=verdict.recommend,
            score=verdict.score,
            concerns=verdict.concerns,
        )
        return verdict

    async def aclose(self) -> None:
        await self._http.aclose()
