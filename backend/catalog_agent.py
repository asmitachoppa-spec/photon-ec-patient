"""AI fallback for resolving a patient-typed allergy or medication name

When the patient inputs their allegies/medications, if we see a misspelling,
a brand name where the patient gave a generic or vice versa, a shortened form, etc.
such that whatever they entered is not in the Photon catalog of known
allergies/medications, instead of just not including that allergy/medication,
we can utilize AI to find out what they meant, and try to look for that in the
catalog instead. The only thing it's allowed to report as a "match" is something a real
search_catalog tool call actually returned from Photon (or Photon's own
mock fallback, if the sandbox is unreachable). If it can't find a reasonable match
after a few tries, it says so, and the caller's existing
unresolved_allergies/unresolved_medications reporting handles it exactly
as it did before this file existed.

Same raw-urllib-to-the-Messages-API pattern as photon-ec-clinician's
assistant.py, kept consistent across this workspace rather than pulling in
the Anthropic SDK for one more small call.

"""

from __future__ import annotations

import os
import json
import logging
import urllib.request
import urllib.error
from typing import Callable, Optional

logger = logging.getLogger("catalog_agent")

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
MAX_TURNS = 4

SEARCH_TOOL = {
    "name": "search_catalog",
    "description": (
        "Search Photon's real catalog for a candidate term. Returns whatever "
        "matches were found (often empty). Call this again with a different "
        "term -- a corrected spelling, a brand name if you were given a "
        "generic (or vice versa), a shortened or more standard form -- if "
        "the previous search came back empty. Only terms that come back "
        "from this tool are real Photon catalog entries; never invent one."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "term": {"type": "string", "description": "The term to search for."}
        },
        "required": ["term"],
    },
}

FINALIZE_TOOL = {
    "name": "finalize_match",
    "description": (
        "Call this exactly once, when you're done: either you found a real "
        "match (pass its id and name exactly as search_catalog returned "
        "them) or you've tried a reasonable number of variations and none "
        "matched (leave matched_id and matched_name null)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "matched_id": {"type": ["string", "null"]},
            "matched_name": {"type": ["string", "null"]},
            "reasoning": {
                "type": "string",
                "description": "One short sentence: what you tried and why you landed here.",
            },
        },
        "required": ["reasoning"],
    },
}


def _post(payload: dict) -> dict:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", api_key)
    req.add_header("anthropic-version", ANTHROPIC_VERSION)
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Anthropic API error {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach Anthropic API: {e.reason}") from e


def try_ai_resolve(
    term: str,
    kind: str,
    search_fn: Callable[[str], dict],
    results_key: str,
) -> Optional[dict]:
    """kind: a short label used only in the prompt ("allergy" or
    "medication"). search_fn(term) is the bound PhotonClient method to call
    for each guess -- search_allergens or search_treatments -- returning
    the same dict shape those already do (e.g. {"allergens": [...],
    "_source": ...}); results_key says which key holds the match list
    ("allergens" or "treatments").

    Returns {"id", "name", "reasoning", "tried"} on a real match, or None
    if the model couldn't find one, gave up, or the API isn't configured/
    reachable -- either way the caller's existing unresolved_* fallback
    handles it exactly as it did before this function existed."""

    system = (
        f"A patient typed a {kind} name on an intake form: {term!r}. A direct "
        f"search of Photon's real {kind} catalog for exactly what they typed "
        "came back with no matches. Your only job is to try a small number "
        "of plausible alternate search terms via the search_catalog tool -- "
        "a common misspelling, a brand name if this looks generic (or vice "
        "versa), a shortened or more standard form -- and call "
        "finalize_match once you've found a real match or tried enough "
        "(2-3 tries is usually plenty; don't keep guessing indefinitely). "
        "You are not making any clinical judgment here, only trying to find "
        "the right row in a lookup table -- never invent an id yourself; "
        "only report one that search_catalog actually returned."
    )

    messages = [
        {"role": "user", "content": f"Find the right {kind} catalog entry for {term!r}."}
    ]
    tried: list[str] = []

    for _ in range(MAX_TURNS):
        try:
            message = _post({
                "model": MODEL,
                "max_tokens": 512,
                "system": system,
                "tools": [SEARCH_TOOL, FINALIZE_TOOL],
                "tool_choice": {"type": "auto"},
                "messages": messages,
            })
        except RuntimeError as e:
            logger.warning("catalog_agent unavailable for %r: %s", term, e)
            return None

        content = message.get("content", [])
        messages.append({"role": "assistant", "content": content})

        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        if not tool_uses:
            # Model responded with plain text instead of calling a tool --
            # treat as giving up, same as an explicit no-match finalize.
            logger.info("catalog_agent gave up on %r without a tool call", term)
            return None

        tool_results = []
        finalized = None
        for block in tool_uses:
            if block["name"] == "search_catalog":
                candidate_term = block["input"].get("term", "")
                tried.append(candidate_term)
                result = search_fn(candidate_term)
                matches = result.get(results_key) or []
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": json.dumps(matches[:5]),
                })
            elif block["name"] == "finalize_match":
                finalized = block["input"]
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": "recorded",
                })

        if finalized is not None:
            if finalized.get("matched_id"):
                logger.info(
                    "catalog_agent resolved %r -> %s (%s) after trying %s",
                    term, finalized.get("matched_name"), finalized.get("matched_id"), tried,
                )
                return {
                    "id": finalized["matched_id"],
                    "name": finalized.get("matched_name"),
                    "reasoning": finalized.get("reasoning", ""),
                    "tried": tried,
                }
            logger.info("catalog_agent could not resolve %r after trying %s", term, tried)
            return None

        messages.append({"role": "user", "content": tool_results})

    logger.warning("catalog_agent gave up on %r after %d turns without finalizing", term, MAX_TURNS)
    return None
