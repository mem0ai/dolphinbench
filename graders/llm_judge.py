"""
LLM-judge grader for DolphinBench.

Per-criterion (per-nugget) evaluation: one LLM call per rubric
criterion, in parallel, against a small focused user prompt. The
system prompt is identical across all calls in a re-grade so providers
that support prompt caching can reuse the prefix.

Pass model (April 2026): every criterion is a gatekeeper. The test
passes iff every criterion in the rubric passes. ``score`` is reported
as a binary ``1.0`` / ``0.0`` mirroring ``passed`` — partial-credit
fractions like 2/3=0.67 reintroduced visual "mostly right" signal even
under all-required grading, so the headline number is now collapsed to
match the binary verdict. Per-criterion diagnostics still live in
``details[]`` / ``passed_checks`` / ``total_checks``. Any
``pass_threshold`` field that survives in a YAML during the migration
is ignored gracefully.

Supports two backends:
  - "openai":   OpenAI-compatible chat completions (OpenRouter, OpenAI proper)
  - "azure":    Azure OpenAI with deployment-based routing (needs api-key header
                and max_completion_tokens for reasoning models like gpt-5)

Backend is selected via DOLPHINBENCH_JUDGE_BACKEND env var (default: "openai").
"""

from __future__ import annotations

import json
import os
from harness.environment import get_setting, openai_config_path
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any


# =========================================================================
# Backend config via env vars
# =========================================================================
# DolphinBench default judge = GPT-5.6 Sol on Azure. We deliberately do
# NOT default to gpt-4o-mini: ~75% of the corpus is judge-graded, so a weak judge
# would cap the whole benchmark (cf. the LoCoMo audit, judges accepting up to 63%
# of wrong answers). Override the env vars only to deliberately swap the model.
BACKEND = os.environ.get("DOLPHINBENCH_JUDGE_BACKEND", "azure").lower()

# Shared defaults
DEFAULT_MAX_TOKENS = int(os.environ.get("DOLPHINBENCH_JUDGE_MAX_TOKENS", "8000"))

# Per-criterion calls are smaller than the omnibus call and benefit from
# a tighter timeout so a single hung judge call doesn't drag the whole
# test re-grade. Override via DOLPHINBENCH_JUDGE_TIMEOUT.
PER_CRITERION_TIMEOUT = int(os.environ.get("DOLPHINBENCH_JUDGE_TIMEOUT", "60"))

# Provider throttles should not become benchmark failures. Retry only
# transport-level transient errors; semantic judge verdicts remain final.
JUDGE_MAX_RETRIES = int(os.environ.get("DOLPHINBENCH_JUDGE_MAX_RETRIES", "5"))
JUDGE_RETRY_BASE_S = float(os.environ.get("DOLPHINBENCH_JUDGE_RETRY_BASE_S", "2.0"))
JUDGE_RETRY_MAX_S = float(os.environ.get("DOLPHINBENCH_JUDGE_RETRY_MAX_S", "30.0"))
RETRYABLE_HTTP_STATUS = {408, 409, 429, 500, 502, 503, 504}

# Defensive bound: a malformed YAML with 100 criteria would otherwise
# fan out 100 LLM calls per test. 20 is well above any human-authored
# rubric in the suite (largest today is 5).
MAX_CRITERIA_PER_TEST = int(os.environ.get("DOLPHINBENCH_JUDGE_MAX_CRITERIA", "20"))

# Bound the parallelism; per-test rubrics are tiny but a runaway suite
# could otherwise open hundreds of sockets at once.
PER_TEST_PARALLELISM = int(os.environ.get("DOLPHINBENCH_JUDGE_PARALLELISM", "8"))

# --- OpenAI-compatible backend (OpenRouter, OpenAI) ---
OPENAI_DEFAULT_MODEL = os.environ.get("DOLPHINBENCH_JUDGE_MODEL", "openai/gpt-4o-mini")
OPENAI_DEFAULT_ENDPOINT = os.environ.get(
    "DOLPHINBENCH_JUDGE_ENDPOINT",
    "https://openrouter.ai/api/v1/chat/completions",
)
OPENAI_API_KEY_ENV = os.environ.get("DOLPHINBENCH_JUDGE_KEY_ENV", "OPENROUTER_API_KEY")

# --- Azure OpenAI backend (DolphinBench default judge: GPT-5.6 Sol) ---
def _azure_creds_from_config() -> tuple[str, str]:
    """Optional Azure config fallback used when env vars are absent.

    The judge builds {endpoint}/openai/deployments/...; config base_url may end
    in /openai/v1, so strip from /openai onward to get the bare endpoint.
    """
    try:
        import yaml as _yaml
        path = openai_config_path()
        cfg = _yaml.safe_load(open(path))
        p = cfg["llm"]["providers"]["openai"]
        return p["base_url"].split("/openai")[0].rstrip("/"), p["api_key"]
    except Exception:
        return "", ""


_CFG_EP, _CFG_KEY = _azure_creds_from_config()
AZURE_ENDPOINT = (os.environ.get("AZURE_OPENAI_ENDPOINT") or _CFG_EP).rstrip("/")
AZURE_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY") or _CFG_KEY
AZURE_DEPLOYMENT = os.environ.get("DOLPHINBENCH_JUDGE_DEPLOYMENT", "gpt-5.6-sol")
AZURE_API_VERSION = os.environ.get("DOLPHINBENCH_JUDGE_API_VERSION", "2024-08-01-preview")
JUDGE_REASONING_EFFORT = os.environ.get(
    "DOLPHINBENCH_JUDGE_REASONING_EFFORT", "medium"
)


# Single-criterion judge prompt. Stable across calls so providers with
# prompt caching can reuse the prefix (the entire system message + the
# leading constant chunk of the user message that follows).
JUDGE_SYSTEM_PROMPT = """You are a strict, impartial rubric grader evaluating \
a memory-evaluation benchmark for AI assistants. You will be given:
  - The USER'S TEST MESSAGE (the prompt sent to the agent)
  - The AGENT'S RESPONSE
  - Optionally, the agent's TOOL CALLS in order (name + args only)
  - A SINGLE CRITERION to judge

Decide binary PASS or FAIL based strictly on the criterion description.
Do not be lenient. Do not give partial credit. Output ONLY valid JSON \
in this exact shape:

{"passed": true, "reasoning": "one short sentence why, quoting the response where relevant"}

No prose outside the JSON, no markdown fences."""


def _retry_delay(exc: urllib.error.HTTPError | urllib.error.URLError, attempt: int) -> float:
    if isinstance(exc, urllib.error.HTTPError):
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        if retry_after:
            try:
                return min(JUDGE_RETRY_MAX_S, max(0.0, float(retry_after)))
            except ValueError:
                pass
    return min(JUDGE_RETRY_MAX_S, JUDGE_RETRY_BASE_S * (2 ** attempt))


def _urlopen_json(req: urllib.request.Request, timeout: int, *, model: str | None = None) -> dict:
    from harness.paid_budget import active_budget
    from harness.provider_capacity import provider_call
    budget = active_budget()
    last_exc: Exception | None = None
    retries = 0 if get_setting("DOLPHINBENCH_AUTHORING_PROVIDER_CAPACITY") else JUDGE_MAX_RETRIES
    for attempt in range(retries + 1):
        call_id = None
        if budget is not None:
            if not model:
                raise ValueError("budgeted judge request must identify its model")
            call_id = budget.reserve(model, json.loads(req.data))
        try:
            with provider_call(model=model or "unknown", request=json.loads(req.data), kind="judge") as transport:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = json.loads(resp.read())
                transport["usage"] = raw.get("usage")
            if budget is not None:
                budget.settle(call_id, raw.get("usage"))
            return raw
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRYABLE_HTTP_STATUS or attempt >= retries:
                raise
            last_exc = exc
        except urllib.error.URLError as exc:
            if attempt >= retries:
                raise
            last_exc = exc
        time.sleep(_retry_delay(last_exc, attempt))
    raise RuntimeError(f"judge retry exhausted: {last_exc!r}")


def _build_per_criterion_prompt(
    response_text: str,
    test_message: str,
    criterion: dict,
    tool_calls: list[dict] | None = None,
) -> str:
    """Build the user prompt for a single-criterion judge call."""
    lines = [
        "TEST MESSAGE (what the user asked the agent):",
        "```",
        test_message.strip(),
        "```",
        "",
        "AGENT RESPONSE:",
        "```",
        response_text.strip(),
        "```",
        "",
    ]
    if tool_calls:
        # Trim to essentials so the prompt stays manageable. We surface
        # tool name + args only — the full result/return value is rarely
        # needed to grade behavior.
        compact = [{"tool": c.get("tool"), "args": c.get("args", {})} for c in tool_calls]
        lines += [
            "TOOL CALLS (in order, name + args only):",
            "```json",
            json.dumps(compact, indent=2, default=str),
            "```",
            "",
        ]
    lines += [
        "CRITERION:",
        f"  id: {criterion['id']}",
        f"  criterion: {criterion['criterion'].strip()}",
        "",
        "Output the JSON verdict now.",
    ]
    return "\n".join(lines)


def _call_openai(system_prompt: str, user_prompt: str, config: dict,
                 timeout: int = 90) -> dict:
    """Call an OpenAI-compatible endpoint (OpenRouter, OpenAI proper)."""
    model = config.get("judge_model", OPENAI_DEFAULT_MODEL)
    endpoint = config.get("judge_endpoint", OPENAI_DEFAULT_ENDPOINT)
    api_key_env = config.get("judge_key_env", OPENAI_API_KEY_ENV)
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        raise RuntimeError(f"Missing env var {api_key_env}")

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/hermes-agent",
            "X-Title": "DolphinBench Benchmark",
        },
        method="POST",
    )
    started = time.monotonic()
    raw = _urlopen_json(req, timeout, model=model)
    from graders.judge_recording import record_response
    record_response({key: value for key, value in body.items() if key != "messages"},
                    body["messages"], raw, (time.monotonic() - started) * 1000)
    text = raw["choices"][0]["message"]["content"]
    return _extract_json(text)


def _call_azure(system_prompt: str, user_prompt: str, config: dict,
                timeout: int = 120) -> dict:
    """Call Azure OpenAI deployment — uses api-key header and
    max_completion_tokens (for reasoning models like gpt-5)."""
    endpoint = config.get("azure_endpoint", AZURE_ENDPOINT).rstrip("/")
    api_key = config.get("azure_api_key", AZURE_API_KEY)
    deployment = config.get("azure_deployment", AZURE_DEPLOYMENT)
    api_version = config.get("azure_api_version", AZURE_API_VERSION)

    if not endpoint:
        raise RuntimeError("Missing AZURE_OPENAI_ENDPOINT env var")
    if not api_key:
        raise RuntimeError("Missing AZURE_OPENAI_API_KEY env var")

    url = (
        f"{endpoint}/openai/deployments/{deployment}"
        f"/chat/completions?api-version={api_version}"
    )
    body = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_completion_tokens": DEFAULT_MAX_TOKENS,
        "reasoning_effort": config.get("reasoning_effort", JUDGE_REASONING_EFFORT),
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "api-key": api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.monotonic()
    raw = _urlopen_json(req, timeout, model=deployment)
    from graders.judge_recording import record_response
    record_response({"model": deployment,
                     **{key: value for key, value in body.items() if key != "messages"}},
                    body["messages"], raw, (time.monotonic() - started) * 1000)

    choice = raw["choices"][0]
    text = choice["message"].get("content") or ""
    if not text.strip():
        # Reasoning model ran out of token budget — surface this clearly
        usage = raw.get("usage", {})
        details = usage.get("completion_tokens_details", {})
        raise RuntimeError(
            f"Azure judge returned empty content "
            f"(finish_reason={choice.get('finish_reason')}, "
            f"reasoning_tokens={details.get('reasoning_tokens')}, "
            f"completion_tokens={usage.get('completion_tokens')})"
        )
    return _extract_json(text)


def _extract_json(text: str) -> dict:
    """Extract a JSON dict from judge output, tolerating code fences."""
    text = text.strip()
    if text.startswith("```"):
        # Strip ```json or ``` fence
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return json.loads(text)


def _call_judge(system_prompt: str, user_prompt: str, config: dict,
                timeout: int | None = None) -> dict:
    """Dispatch to the right backend."""
    from graders.judge_recording import replay_response
    recorded = replay_response(system_prompt, user_prompt, config)
    if recorded is not None:
        return recorded
    backend = config.get("backend", BACKEND)
    if backend == "azure":
        return _call_azure(
            system_prompt, user_prompt, config,
            timeout=timeout if timeout is not None else 120,
        )
    return _call_openai(
        system_prompt, user_prompt, config,
        timeout=timeout if timeout is not None else 90,
    )


def _judge_one_criterion(
    criterion: dict,
    response_text: str,
    test_message: str,
    config: dict,
    tool_calls: list[dict] | None,
) -> dict:
    """Run one judge call for a single criterion. Returns a dict in the
    existing ``details[]`` shape: ``{name, ok, reason}``. On API error
    the criterion is recorded as failed with a ``judge_call_error:`` reason
    so the rest of the rubric can still be judged."""
    try:
        user_prompt = _build_per_criterion_prompt(
            response_text, test_message, criterion, tool_calls=tool_calls,
        )
        parsed = _call_judge(
            JUDGE_SYSTEM_PROMPT, user_prompt, config,
            timeout=PER_CRITERION_TIMEOUT,
        )
    except Exception as e:
        return {
            "name": criterion["id"],
            "ok": False,
            "reason": f"judge_call_error: {e}",
        }

    ok = bool(parsed.get("passed"))
    # Accept "reasoning" (new shape) or "reason" (legacy field-judge shape)
    reason = parsed.get("reasoning") or parsed.get("reason") or "(no reasoning given)"
    return {"name": criterion["id"], "ok": ok, "reason": reason}


def grade_llm_judge(
    response_text: str,
    test_message: str,
    config: dict,
    tool_calls: list[dict] | None = None,
) -> dict:
    """
    Grade using an LLM judge.

    Per-criterion model: one LLM call per rubric entry, fanned out in
    parallel. Pass = every criterion passes. ``score`` is binary
    (``1.0`` if every criterion passed, else ``0.0``) so the headline
    number matches ``passed``; per-criterion outcomes remain in
    ``details[]`` / ``passed_checks`` / ``total_checks`` for diagnosis.

    config:
        rubric:         list of {id, criterion}
        backend:        optional override, "openai" or "azure"
        pass_threshold: ignored if present (graceful migration support).

    ``tool_calls``: optional list of tool call records from the test run.
    When provided, each per-criterion judge sees a compact "TOOL CALLS"
    section in its prompt — useful for criteria that need to grade
    *which* tool was used or *which* recipient was contacted.
    """
    rubric = config.get("rubric", []) or []
    backend = config.get("backend", BACKEND)

    # Defensive bound — runaway YAMLs don't get to spend $$$.
    if len(rubric) > MAX_CRITERIA_PER_TEST:
        rubric_capped = rubric[:MAX_CRITERIA_PER_TEST]
        truncation_note = (
            f"rubric truncated to first {MAX_CRITERIA_PER_TEST} criteria "
            f"(had {len(rubric)})"
        )
    else:
        rubric_capped = rubric
        truncation_note = None

    judge_label = (
        f"azure:{config.get('azure_deployment', AZURE_DEPLOYMENT)}"
        if backend == "azure"
        else config.get("judge_model", OPENAI_DEFAULT_MODEL)
    )

    # Empty rubric: report explicit failure rather than a vacuous pass.
    if not rubric_capped:
        return {
            "grader_type": "llm_judge",
            "passed": False,
            "score": 0.0,
            "details": [{"name": "rubric", "ok": False,
                         "reason": "empty rubric"}],
            "total_checks": 0,
            "passed_checks": 0,
            "judge_model": judge_label,
            "backend": backend,
        }

    # Fan out one call per criterion. Order of details preserved by
    # iterating in rubric order over the future results.
    details_by_id: dict[str, dict] = {}
    parallelism = max(1, min(PER_TEST_PARALLELISM, len(rubric_capped)))
    with ThreadPoolExecutor(max_workers=parallelism) as ex:
        futures = {
            ex.submit(
                _judge_one_criterion,
                c, response_text, test_message, config, tool_calls,
            ): c["id"]
            for c in rubric_capped
        }
        for fut, cid in futures.items():
            try:
                details_by_id[cid] = fut.result()
            except Exception as e:
                # Belt-and-suspenders — _judge_one_criterion already
                # catches its own exceptions. This handles the unlikely
                # case where the executor itself raises.
                details_by_id[cid] = {
                    "name": cid,
                    "ok": False,
                    "reason": f"judge_call_error: {e}",
                }

    details: list[dict] = [details_by_id[c["id"]] for c in rubric_capped]
    passed_count = sum(1 for d in details if d["ok"])
    total = len(details)
    all_passed = passed_count == total and total > 0

    result = {
        "grader_type": "llm_judge",
        "passed": all_passed,
        # Headline score is binary under all-required grading — partial
        # ratios like 2/3 = 0.67 leak a "mostly right" signal that the
        # gatekeeper model is meant to suppress. Per-criterion outcomes
        # live in details[] / passed_checks / total_checks below.
        "score": 1.0 if all_passed else 0.0,
        "details": details,
        "total_checks": total,
        "passed_checks": passed_count,
        "judge_model": judge_label,
        "judge_reasoning_effort": config.get(
            "reasoning_effort", JUDGE_REASONING_EFFORT
        ),
        "backend": backend,
    }
    if truncation_note:
        result["truncation_note"] = truncation_note
    return result


# =========================================================================
# Field-level judge — used by the mechanical grader's field_llm_judge
# assertion type to evaluate a single extracted value (e.g. a tool call
# arg) against a free-form criterion.
# =========================================================================

FIELD_JUDGE_SYSTEM_PROMPT = """You are an impartial grader evaluating \
whether a completed action satisfies a CRITERION. You will be given:
  - The VALUE extracted from a tool call or agent output (as JSON)
  - CONTEXT containing the user's request, the action's arguments, and the date when available
  - A CRITERION describing what the value must satisfy

Decide binary PASS or FAIL strictly on the criterion. No partial credit. \
Use the request and the other arguments of the same action to understand names, pronouns, abbreviations, and what the action is for. Accept different wording that communicates the same required meaning. Do not require a message to repeat its recipient's name. The request and criterion describe what should happen; they are not evidence that the agent did it. Do not supply a missing answer from them or from a different action. Fail when the completed action omits, contradicts, or leaves unclear a required meaning. \
Output ONLY valid JSON in this shape:

{"passed": true, "reason": "a concise explanation naming the exact supporting text or the exact missing requirement"}

No prose, no markdown fences."""


ACTION_JUDGE_SYSTEM_PROMPT = """Evaluate each named criterion independently
against its extracted value and the complete arguments of this SAME tool call.
Return one Boolean verdict and a concise evidence-based reason per check_id.
Do not combine criteria into a holistic score or add requirements.

Use the request and same-call arguments to resolve names, pronouns, abbreviations,
and intended authorship. Accept equivalent wording that communicates the required
meaning. A first-person document written for the requesting user can refer to
that user as I; do not demand repetition of a name when authorship is clear.
The request and criteria are not evidence that the action included an answer.
Missing, incomplete, ambiguous, contradictory, or negated REQUIRED meaning fails.
Do not confuse mentioning a forbidden claim in an explicit denial with making
that claim. Do not obtain missing content from another call or criterion.

Return only JSON: {"results": [{"check_id": "supplied ID", "passed": true,
"reason": "exact supporting text or missing requirement"}]}.
Include every supplied check_id exactly once and no additional IDs.
"""


def judge_action_fields(*, call: dict, fields: list[dict], request: str, today: str) -> dict:
    """Version-2 semantic comparisons, preserving distinct per-check evidence."""
    expected = {row["check_id"] for row in fields}
    if not fields or len(expected) != len(fields):
        raise ValueError("action judge needs distinct supplied checks")
    payload = {"user_request": request, "today": today, "tool": call["tool"],
               "arguments": call.get("args", {}), "checks": fields}
    parsed = _call_judge(ACTION_JUDGE_SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False), {})
    rows = parsed.get("results")
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise ValueError("action judge returned incomplete results")
    result = {}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"check_id", "passed", "reason"}
                or row["check_id"] not in expected or row["check_id"] in result
                or type(row["passed"]) is not bool
                or not isinstance(row["reason"], str) or not row["reason"].strip()):
            raise ValueError("action judge returned malformed or duplicate results")
        result[row["check_id"]] = {"ok": row["passed"], "reason": row["reason"]}
    return result


def judge_field_value(
    value: Any,
    criterion: str,
    context: str = "",
    config: dict | None = None,
) -> dict:
    """Ask the judge whether ``value`` satisfies ``criterion``.

    Returns a dict ``{"ok": bool, "reason": str}``. Failures in the
    judge call itself (network, parse) are surfaced as ``ok=False`` with
    the error in ``reason`` so the caller can decide how to score.
    """
    cfg = dict(config or {})
    user_prompt = (
        f"VALUE (as JSON):\n{json.dumps(value, default=str)}\n\n"
    )
    if context:
        user_prompt += f"CONTEXT: {context}\n\n"
    user_prompt += f"CRITERION:\n{criterion}\n\n" \
                   "Return the JSON verdict now."

    try:
        parsed = _call_judge(FIELD_JUDGE_SYSTEM_PROMPT, user_prompt, cfg)
    except Exception as e:
        if cfg.get("raise_on_error"):
            raise
        return {"ok": False, "reason": f"judge call failed: {e}"}

    passed = bool(parsed.get("passed"))
    reason = parsed.get("reason", "(no reason given)")
    return {"ok": passed, "reason": reason}
