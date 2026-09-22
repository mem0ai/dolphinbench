"""
DolphinBench cost computation module.

Loads ``pricing/latest.json`` and computes per-call / per-session USD cost
from provider-reported usage blocks. Stdlib only.

Public surface:
    UnknownModelError
    load_pricing(path=None) -> dict
    pricing_version(path=None) -> str
    compute_call_cost(usage, model_id, pricing) -> float
    compute_session_cost(api_calls, model_id, pricing) -> float

Per spec §6.3.2:

    call_cost = input_tokens × input_cost_per_token
              + cache_creation_input_tokens × cache_creation_input_token_cost
              + cache_read_input_tokens × cache_read_input_token_cost
              + output_tokens × output_cost_per_token

If the pricing snapshot lacks cache fields for a model, all input is treated
as fresh (upper-bound cost — see spec §6.3.3).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

DOLPHINBENCH_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PRICING_PATH = DOLPHINBENCH_ROOT / "pricing" / "latest.json"


class UnknownModelError(KeyError):
    """Raised when a model_id is not present in the pricing snapshot.

    Caller decides whether to fall back to a self-reported pricing.json
    (per spec §6.3.4) or skip cost reporting entirely.
    """


def _normalize_pricing_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Accept either a bare snapshot or the latest.json pointer envelope.

    ``snapshot_pricing.py`` writes:
      - ``YYYY-MM-DD.json``: bare snapshot dict with ``snapshot_date``,
        ``models``, etc.
      - ``latest.json``: pointer envelope with ``points_to``,
        ``snapshot``, ``updated_at``.

    Both should resolve to the same in-memory shape.
    """
    if "snapshot" in raw and isinstance(raw["snapshot"], dict):
        return raw["snapshot"]
    return raw


def load_pricing(path: str | Path | None = None) -> dict[str, Any]:
    """Load a pricing snapshot. Defaults to ``pricing/latest.json``.

    Returns the snapshot dict (with ``models``, ``snapshot_date``, etc.).
    """
    p = Path(path) if path else DEFAULT_PRICING_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"Pricing snapshot not found at {p}. "
            f"Add a pricing/latest.json snapshot before reporting cost."
        )
    raw = json.loads(p.read_text())
    return _normalize_pricing_payload(raw)


def pricing_version(path: str | Path | None = None) -> str:
    """Return the snapshot date string (YYYY-MM-DD)."""
    pricing = load_pricing(path)
    return pricing.get("snapshot_date", "unknown")


def _model_entry(pricing: dict[str, Any], model_id: str) -> dict[str, Any]:
    """Look up a model's pricing entry, with prefix-fallback heuristics.

    LiteLLM keys vary by provider routing — for example:
      - "claude-sonnet-4-5" (bare)
      - "openrouter/anthropic/claude-sonnet-4.5" (routed)
      - "anthropic/claude-sonnet-4.5" (provider-prefixed)
    Try the exact key first, then strip common provider prefixes.
    """
    models = pricing.get("models") or {}
    if model_id in models:
        return models[model_id]
    # Try stripped variations
    if "/" in model_id:
        bare = model_id.rsplit("/", 1)[-1]
        if bare in models:
            return models[bare]
    # Try with common prefixes
    for prefix in ("anthropic/", "openai/", "google/", "openrouter/"):
        candidate = prefix + model_id
        if candidate in models:
            return models[candidate]
    raise UnknownModelError(model_id)


def _to_int(v: Any) -> int:
    """Coerce a usage value to int; treat None/missing as 0."""
    if v is None:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _long_context_rate(entry: dict[str, Any], field: str, request_input_tokens: int) -> float:
    """Return the rate selected by the snapshot's input-length tiers.

    A model page can price an entire request differently after a context
    threshold. The snapshot represents that with fields such as
    ``input_cost_per_token_above_272k_tokens``. The same input threshold
    selects input, output, cache-read, and cache-write rates.
    """
    selected_field = field
    selected_threshold = -1
    prefix = f"{field}_above_"
    for key in entry:
        match = re.fullmatch(re.escape(prefix) + r"(\d+)k_tokens", key)
        if not match:
            continue
        threshold = int(match.group(1)) * 1000
        if request_input_tokens > threshold and threshold > selected_threshold:
            selected_field = key
            selected_threshold = threshold
    return float(entry.get(selected_field) or 0)


def compute_call_cost(
    usage: dict[str, Any],
    model_id: str,
    pricing: dict[str, Any],
) -> float:
    """Compute USD cost for a single LLM API call.

    ``usage`` must include at least ``input_tokens`` and ``output_tokens``.
    May also include ``cache_creation_input_tokens`` and
    ``cache_read_input_tokens``; if the pricing snapshot has cache fields
    those are used, otherwise cache tokens roll into the input bucket
    (treated as fresh — upper-bound cost, per spec §6.3.3).

    Raises ``UnknownModelError`` if ``model_id`` is not in the snapshot.
    """
    entry = _model_entry(pricing, model_id)

    input_tokens = _to_int(usage.get("input_tokens"))
    output_tokens = _to_int(usage.get("output_tokens"))
    cache_creation = _to_int(usage.get("cache_creation_input_tokens"))
    cache_read = _to_int(usage.get("cache_read_input_tokens"))

    request_input_tokens = input_tokens + cache_creation + cache_read
    in_cost_per = _long_context_rate(
        entry, "input_cost_per_token", request_input_tokens,
    )
    out_cost_per = _long_context_rate(
        entry, "output_cost_per_token", request_input_tokens,
    )
    cache_read_cost_per = _long_context_rate(
        entry, "cache_read_input_token_cost", request_input_tokens,
    ) if "cache_read_input_token_cost" in entry else None
    cache_create_cost_per = _long_context_rate(
        entry, "cache_creation_input_token_cost", request_input_tokens,
    ) if "cache_creation_input_token_cost" in entry else None

    cost = input_tokens * in_cost_per + output_tokens * out_cost_per

    if cache_read_cost_per is not None:
        cost += cache_read * float(cache_read_cost_per)
    else:
        # Fall back: treat cache_read tokens as fresh input
        cost += cache_read * in_cost_per

    if cache_create_cost_per is not None:
        cost += cache_creation * float(cache_create_cost_per)
    else:
        # Fall back: treat cache_creation tokens as fresh input
        cost += cache_creation * in_cost_per

    return cost


def compute_session_cost(
    api_calls: list[dict[str, Any]],
    model_id: str,
    pricing: dict[str, Any],
) -> float:
    """Sum per-call costs across an entire session/test.

    Each entry in ``api_calls`` should be a usage dict (same shape as
    accepted by ``compute_call_cost``). If an entry has its own
    ``model_id`` key, it overrides the default ``model_id`` argument
    (useful for multi-model systems — spec §6.3.5).
    """
    total = 0.0
    for call in api_calls or []:
        usage = call.get("usage", call)  # accept either {usage:{...}} or flat
        mid = call.get("model_id", model_id)
        total += compute_call_cost(usage, mid, pricing)
    return total


__all__ = [
    "UnknownModelError",
    "load_pricing",
    "pricing_version",
    "compute_call_cost",
    "compute_session_cost",
]
