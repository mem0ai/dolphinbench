"""Oracle preview + per-criterion grading for the TDD mining loop.

This is a 1-run, no-consensus version of audit_realism's pipeline.
Imports from `harness.oracle` (NEVER touches it) and the production
grader (`graders.mechanical._evaluate_assertion`).
"""
from __future__ import annotations

import json
import os
from harness.environment import get_setting, openai_config_path
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from openai import OpenAI


_DOLPHINBENCH_ROOT = Path(__file__).resolve().parent.parent.parent
_MOCK_DIR = _DOLPHINBENCH_ROOT / "mock_mcp"
# Honor DOLPHINBENCH_STATE_PATH / DOLPHINBENCH_LOG_PATH — MUST match oracle.py, or the reset
# truncates a different file than the Oracle/mock-server actually use, and the
# tool log accumulates across shots+tests (every later test falsely "leaks").
_MOCK_STATE = Path(get_setting("DOLPHINBENCH_STATE_PATH", str(_MOCK_DIR / "state.json")))
_MOCK_LOG = Path(get_setting("DOLPHINBENCH_LOG_PATH", str(_MOCK_DIR / "calls.jsonl")))

_MOCK_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Oracle preview
# ---------------------------------------------------------------------------

def _reset_mock_state(persona_id: str, state_override: dict | None = None) -> None:
    # state_override = the per-test assembled state (the fold). When given, the
    # gate runs the Oracle against THAT, not the shared baseline -- this is what
    # makes G2 (memory-necessity) meaningful per test.
    if state_override is not None:
        import json as _json
        _MOCK_STATE.write_text(_json.dumps(state_override))
    else:
        baseline = _MOCK_DIR / "state" / f"{persona_id}_baseline.json"
        if baseline.exists():
            shutil.copy(baseline, _MOCK_STATE)
    open(_MOCK_LOG, "w").close()


def oracle_preview(
    candidate: dict[str, Any],
    persona_id: str,
    *,
    oracle_model: str = "gpt-5.4",
    max_turns: int = 10,
    with_memory: bool = True,
    state_override: dict | None = None,
    style_hint: str = "",
) -> dict[str, Any]:
    """Run the locked Oracle once. Returns the run_oracle dict with
    `tool_trace`, `response_text`, `tool_calls`.

    ``with_memory=False`` runs the G2 counterfactual: the Oracle's
    seed blocks are withheld (tools/state stay available). If the
    Oracle still satisfies the rubric, the answer leaked via the query or
    a state read, not memory.
    """
    # Lazy import; oracle imports mock_mcp/server.py via DOLPHINBENCH_PERSONA env.
    os.environ["DOLPHINBENCH_PERSONA"] = persona_id
    from harness import oracle as _oracle  # noqa: E402

    with _MOCK_LOCK:
        _reset_mock_state(persona_id, state_override)
        result = _oracle.run_oracle(
            candidate, persona_id, model=oracle_model, max_turns=max_turns,
            with_memory=with_memory, style_hint=style_hint,
        )
    return result


def _is_infra_error(run: dict[str, Any]) -> bool:
    """True if this oracle run failed for an INFRASTRUCTURE reason (the LLM call
    itself errored after retries — e.g. a persistent 429), as opposed to the
    oracle genuinely not solving the task. A tool-exec error is NOT infra — that
    is legitimate task behavior. Infra-errored runs must never be graded as a
    task failure, or rate-limiting silently masquerades as 'test invalid' (and
    would depress backend scores the same way)."""
    for e in (run.get("errors") or []):
        if "LLM call failed" in str(e):
            return True
    return False


def _oracle_shot_robust(
    candidate: dict[str, Any], persona_id: str, *,
    with_memory: bool, state_override: dict | None, style_hint: str,
    oracle_model: str, max_turns: int,
    infra_retries: int = 3, infra_sleep: float = 8.0,
) -> dict[str, Any]:
    """One oracle shot that RE-RUNS on infra error rather than letting a starved
    (rate-limited) call count as a task failure. If it still errors after the
    retries, the run is tagged ``_infra_errored`` so the caller can mark the
    verdict indeterminate instead of culling a good test."""
    r: dict[str, Any] = {}
    for attempt in range(infra_retries + 1):
        r = oracle_preview(
            candidate, persona_id, oracle_model=oracle_model, max_turns=max_turns,
            with_memory=with_memory, state_override=state_override, style_hint=style_hint,
        )
        if not _is_infra_error(r):
            return r
        if attempt < infra_retries:
            time.sleep(infra_sleep)
    r["_infra_errored"] = True
    return r


def oracle_preview_multishot(
    candidate: dict[str, Any],
    persona_id: str,
    *,
    oracle_model: str = "gpt-5.4",
    max_turns: int = 10,
    shots: int = 3,
    inter_shot_sleep: float = 1.0,
    with_memory: bool = True,
    state_override: dict | None = None,
) -> list[dict[str, Any]]:
    """Run the locked Oracle ``shots`` times sequentially.

    The mock_mcp state is reset before each run (already done inside
    ``oracle_preview``) so the runs are independent. A small ``time.sleep``
    is inserted between shots to give Azure OpenAI's rate limiter some
    breathing room — Oracle uses Azure OpenAI which can 429 under load.

    ``with_memory`` is forwarded to each run (False = G2 counterfactual).

    G1 phrasing diversity: shot 0 runs canonical; later with-memory shots
    carry a style hint telling the oracle to word things naturally in its
    own phrasing. The shots then sample DIFFERENT natural wordings of
    correct behavior, so a rubric that only accepts one phrasing
    (paraphrase-brittle regex / over-required token) fails G1 instead of
    shipping. G2 shots never get the hint (no memory text to rephrase).

    Returns a list of run_oracle result dicts in execution order.
    """
    if shots < 1:
        raise ValueError(f"shots must be >= 1 (got {shots})")
    _STYLE = ("When writing any message, document, or tool-call text, "
              "phrase things naturally in your own words — do not copy "
              "phrasing verbatim from the context above.")
    results: list[dict[str, Any]] = []
    for i in range(shots):
        if i > 0 and inter_shot_sleep > 0:
            time.sleep(inter_shot_sleep)
        results.append(
            _oracle_shot_robust(
                candidate,
                persona_id,
                oracle_model=oracle_model,
                max_turns=max_turns,
                with_memory=with_memory,
                state_override=state_override,
                style_hint=(_STYLE if (with_memory and i > 0) else ""),
            )
        )
    return results


def aggregate_multishot_grades(
    candidate: dict[str, Any],
    oracle_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Grade each oracle run and aggregate into a multi-shot verdict.

    Returns a dict with:
      - per_run_criteria:    list[list[dict]] (criteria per run)
      - per_run_all_pass:    list[bool]
      - runs_passed_all:     int
      - runs_completed:      int
      - failure_pattern:     dict[str, int]  criterion_id -> fail count
      - all_runs_pass:       bool  (every criterion passes in every run)
      - worst_run_index:     int   index of run with most failing criteria
      - worst_run_criteria:  list[dict]   that run's per-criterion verdicts
    """
    per_run_criteria: list[list[dict]] = []
    per_run_all_pass: list[bool] = []
    failure_pattern: dict[str, int] = {}

    for res in oracle_results:
        crits = grade_against_oracle(candidate, res)
        per_run_criteria.append(crits)
        per_run_all_pass.append(bool(crits) and all(c.get("passes") for c in crits))
        for c in crits:
            if not c.get("passes"):
                cid = c.get("id", "?")
                failure_pattern[cid] = failure_pattern.get(cid, 0) + 1

    # Worst run = the one with most failing criteria (ties broken by first).
    worst_idx = 0
    worst_fail_count = -1
    for i, crits in enumerate(per_run_criteria):
        fails = sum(1 for c in crits if not c.get("passes"))
        if fails > worst_fail_count:
            worst_fail_count = fails
            worst_idx = i

    all_runs_pass = bool(per_run_all_pass) and all(per_run_all_pass)
    return {
        "per_run_criteria": per_run_criteria,
        "per_run_all_pass": per_run_all_pass,
        "runs_completed": len(oracle_results),
        "runs_passed_all": sum(1 for ok in per_run_all_pass if ok),
        "failure_pattern": failure_pattern,
        "all_runs_pass": all_runs_pass,
        "worst_run_index": worst_idx,
        "worst_run_criteria": (per_run_criteria[worst_idx]
                                if per_run_criteria else []),
    }


# ---------------------------------------------------------------------------
# Validity gate — G1 (solvable WITH memory) ∧ G2 (unsolvable WITHOUT memory).
# This is the single primitive the write-time gate, the after-edit re-check,
# and the retrofit labeler all call. A test is VALID iff it requires memory.
# ---------------------------------------------------------------------------

def prod_grade_result(candidate: dict[str, Any],
                      oracle_result: dict[str, Any]) -> dict[str, Any] | None:
    """Return the production grader's complete result for one Oracle run."""
    import sys
    if str(_DOLPHINBENCH_ROOT) not in sys.path:
        sys.path.insert(0, str(_DOLPHINBENCH_ROOT))
    from graders.mechanical import grade_tool_trace, grade_hybrid
    try:
        from graders.mechanical import grade_llm_judge
    except Exception:
        grade_llm_judge = None

    g = candidate.get("grade") or {}
    typ = g.get("type")
    cfg = g.get("config") or {}
    trace = oracle_result.get("tool_trace") or []
    resp = oracle_result.get("response_text") or ""
    if typ == "tool_trace":
        return grade_tool_trace(trace, cfg, test_message=candidate.get("test", ""))
    if typ == "hybrid":
        return grade_hybrid(
            resp, trace, cfg, test_message=candidate.get("test", "")
        )
    if typ == "llm_judge" and grade_llm_judge:
        try:
            return grade_llm_judge(
                resp,
                candidate.get("test", ""),
                cfg,
                tool_calls=oracle_result.get("tool_calls"),
            )
        except Exception:
            return None
    return None


def prod_grade_passed(candidate: dict[str, Any],
                      oracle_result: dict[str, Any]) -> bool | None:
    """Return the production grader's Boolean verdict for one Oracle run."""
    result = prod_grade_result(candidate, oracle_result)
    return bool(result.get("passed")) if isinstance(result, dict) else None


def validity_verdict(
    candidate: dict[str, Any],
    persona_id: str,
    *,
    oracle_model: str = "gpt-5.4",
    max_turns: int = 10,
    g1_shots: int = 2,
    g2_shots: int = 2,
    g1_min_pass: int = 2,
    inter_shot_sleep: float = 1.0,
    state_override: dict | None = None,
) -> dict[str, Any]:
    """Run both gates and return a structured verdict, graded with the
    PRODUCTION grader (``prod_grade_passed``).

    Clean 2/2 bar (both directions must hold on every shot):
    G1 — reliably-solvable: WITH memory, the test must pass ALL ``g1_shots``
         (default 2/2). A frontier oracle that can't solve a test *reliably*
         signals the test is dirty (ambiguous query, brittle grader, ungrounded
         state), not just unlucky — so we require reliable success, and dirty
         tests are repaired or dropped rather than shipped on a lucky 1-of-N.
    G2 — memory-necessity: with memory withheld (tools/state still available),
         the test must FAIL every one of ``g2_shots`` shots. Passing ANY no-
         memory shot ⇒ leaked (query or state channel). The structural leak
         pre-checks (answer not in query/state) catch most leaks before the
         oracle; these shots confirm it.

    ``valid`` iff G1 holds AND G2 holds (no leak). Leak takes precedence in the
    label, since a leaked test's actionable defect is the leak.
    """
    g1_runs = oracle_preview_multishot(
        candidate, persona_id, oracle_model=oracle_model, max_turns=max_turns,
        shots=g1_shots, inter_shot_sleep=inter_shot_sleep, with_memory=True,
        state_override=state_override,
    )
    if any(r.get("_infra_errored") for r in g1_runs):
        return {
            "valid": False, "verdict": "infra_error",
            "g1_pass": False, "g2_leaked": False,
            "g1_pass_count": 0, "g2_pass_count": 0,
            "g1_shots": len(g1_runs), "g2_shots": 0,
            "g1_runs": g1_runs, "g2_runs": [],
        }
    g1_grades = [prod_grade_passed(candidate, r) for r in g1_runs]
    g1_pass_count = sum(1 for g in g1_grades if g is True)
    g1_pass = g1_pass_count >= g1_min_pass

    if not g1_pass:
        # G1 failed -> the verdict is "unsolvable" regardless of leakage, so
        # skip the G2 runs entirely (saves 2 oracle runs on every reject).
        return {
            "valid": False, "verdict": "unsolvable_with_memory",
            "g1_pass": False, "g2_leaked": False,
            "g1_pass_count": g1_pass_count, "g2_pass_count": 0,
            "g1_shots": len(g1_grades), "g2_shots": 0,
            "g1_grades": g1_grades, "g1_runs": g1_runs, "g2_runs": [],
        }

    g2_runs = oracle_preview_multishot(
        candidate, persona_id, oracle_model=oracle_model, max_turns=max_turns,
        shots=g2_shots, inter_shot_sleep=inter_shot_sleep, with_memory=False,
        state_override=state_override,
    )
    if any(r.get("_infra_errored") for r in g2_runs):
        return {
            "valid": False, "verdict": "infra_error",
            "g1_pass": True, "g2_leaked": False,
            "g1_pass_count": g1_pass_count, "g2_pass_count": 0,
            "g1_shots": len(g1_grades), "g2_shots": len(g2_runs),
            "g1_grades": g1_grades, "g1_runs": g1_runs, "g2_runs": g2_runs,
        }
    g2_grades = [prod_grade_passed(candidate, r) for r in g2_runs]
    g2_pass_count = sum(1 for g in g2_grades if g is True)
    g2_leaked = g2_pass_count >= 1

    valid = bool(g1_pass and not g2_leaked)
    if g2_leaked:
        verdict = "leaks_without_memory"     # Class A — query/state channel
    elif not g1_pass:
        verdict = "unsolvable_with_memory"   # Class B/C — never passes even with memory
    else:
        verdict = "valid"
    return {
        "valid": valid,
        "verdict": verdict,
        "g1_pass": g1_pass,
        "g2_leaked": g2_leaked,
        "g1_pass_count": g1_pass_count,
        "g2_pass_count": g2_pass_count,
        "g1_shots": len(g1_grades),
        "g2_shots": len(g2_grades),
        "g1_grades": g1_grades,
        "g2_grades": g2_grades,
        "g1_runs": g1_runs,
        "g2_runs": g2_runs,
    }


# ---------------------------------------------------------------------------
# Grader — mirrors audit_realism's per-run grading.
# ---------------------------------------------------------------------------

_PROD_EVALUATE = None
_JUDGE_CLIENT: OpenAI | None = None
_JUDGE_MODEL = "gpt-5.4"


def _prod_evaluate():
    global _PROD_EVALUATE
    if _PROD_EVALUATE is None:
        import sys
        if str(_DOLPHINBENCH_ROOT) not in sys.path:
            sys.path.insert(0, str(_DOLPHINBENCH_ROOT))
        from graders.mechanical import _evaluate_assertion as _ev  # noqa: E402
        _PROD_EVALUATE = _ev
    return _PROD_EVALUATE


def _judge_client() -> OpenAI:
    """Azure-backed judge for LLM-judge criteria + rubric grading."""
    global _JUDGE_CLIENT
    if _JUDGE_CLIENT is None:
        api_key = os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
        base_url = (
            os.environ.get("AZURE_OPENAI_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
        )
        if not (api_key and base_url):
            import yaml
            path = openai_config_path()
            cfg = yaml.safe_load(path.read_text())
            p = cfg["llm"]["providers"]["openai"]
            api_key, base_url = p["api_key"], p["base_url"]
        _JUDGE_CLIENT = OpenAI(api_key=api_key, base_url=base_url,
                               timeout=120.0, max_retries=5)
    return _JUDGE_CLIENT


def _parse_json_blob(raw: str):
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        try:
            return json.loads(m.group(0).replace("\n", " ").replace("\t", " "))
        except Exception:
            return None


_FIELD_JUDGE_SYSTEM = (
    "You are a strict, impartial grader evaluating whether a single VALUE "
    "satisfies a single CRITERION. Use only what the VALUE explicitly states. "
    "Accept a clear paraphrase of the complete required meaning. Fail if any "
    "required distinction is omitted, reversed, or ambiguous. Do not supply "
    "missing information by inference. Output ONLY valid JSON: "
    '{"passed": true|false, "reason": "one short sentence"}.'
)

_RUBRIC_JUDGE_SYSTEM = (
    "You are a strict rubric grader. Decide whether the response satisfies "
    "the criterion. Output ONLY valid JSON: "
    '{"passes": true|false, "reason": "one short sentence"}.'
)


def _llm_call(system: str, user: str) -> str:
    resp = _judge_client().chat.completions.create(
        model=_JUDGE_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content or ""


def _eval_field_llm_judge(assertion: dict[str, Any], trace: list[dict]) -> tuple[bool, str]:
    import sys
    if str(_DOLPHINBENCH_ROOT) not in sys.path:
        sys.path.insert(0, str(_DOLPHINBENCH_ROOT))
    from graders.mechanical import _walk_path  # noqa: E402

    tool = assertion.get("tool")
    path = assertion.get("path")
    criterion = assertion.get("criterion", "")
    context = assertion.get("context", "")
    check_calls = [c for c in trace if c.get("tool") == tool] if tool else list(trace)
    if not check_calls:
        return False, "no tool calls to judge"

    for c in check_calls:
        actual = _walk_path(c, path)
        user = (
            f"VALUE (as JSON):\n{json.dumps(actual, default=str)}\n\n"
            + (f"CONTEXT: {context}\n\n" if context else "")
            + f"CRITERION:\n{criterion}\n\nReturn the JSON verdict now."
        )
        raw = _llm_call(_FIELD_JUDGE_SYSTEM, user)
        parsed = _parse_json_blob(raw)
        if parsed and bool(parsed.get("passed", parsed.get("ok", False))):
            return True, parsed.get("reason") or "llm-judge pass"
    return False, "llm-judge: no calls matched"


def _eval_assertion(assertion: dict[str, Any], trace: list[dict]) -> tuple[bool, str]:
    try:
        if assertion.get("type") == "field_llm_judge":
            return _eval_field_llm_judge(assertion, trace)
        ev = _prod_evaluate()
        ok, reason = ev(assertion, trace, [], None)
        return bool(ok), reason
    except Exception as e:
        return False, f"prod-grader eval error: {e}"


def _grade_rubric_criterion(criterion_text: str, response: str, trace: list[dict]) -> dict:
    trace_str = json.dumps(trace, indent=2)[:6000]
    user = (
        f"FINAL ANSWER:\n{response}\n\n"
        f"TOOL-CALL TRACE:\n{trace_str}\n\n"
        f"CRITERION:\n{criterion_text}\n\n"
        "Does the agent's behaviour (final answer + tool calls) satisfy the criterion?"
    )
    raw = _llm_call(_RUBRIC_JUDGE_SYSTEM, user)
    parsed = _parse_json_blob(raw)
    if not parsed:
        return {"passes": False, "reason": f"unparseable rubric grader: {raw[:200]}"}
    return parsed


def _eval_regex_block(regex_cfg: dict[str, Any], response: str) -> list[dict]:
    out: list[dict] = []
    required = regex_cfg.get("patterns_required", []) or []
    forbidden = regex_cfg.get("patterns_forbidden", []) or []
    for i, p in enumerate(required):
        pat = p.get("pattern", "")
        flags = re.MULTILINE if p.get("multiline") else 0
        try:
            rx = re.compile(pat, flags)
        except re.error as e:
            out.append({"id": f"required_{i}", "criterion": f"required: {pat}",
                        "passes": False, "reason": f"bad regex: {e}"})
            continue
        matches = rx.findall(response)
        n = len(matches)
        mn = p.get("min_count", 1)
        mx = p.get("max_count")
        ok = n >= mn and (mx is None or n <= mx)
        out.append({"id": f"required_{i}", "criterion": f"required: {pat}",
                    "passes": ok,
                    "reason": f"matched {n} times" + ("" if ok else " (out of bounds)")})
    for i, p in enumerate(forbidden):
        pat = p.get("pattern", "")
        try:
            rx = re.compile(pat)
        except re.error as e:
            out.append({"id": f"forbidden_{i}", "criterion": f"forbidden: {pat}",
                        "passes": False, "reason": f"bad regex: {e}"})
            continue
        matches = rx.findall(response)
        ok = len(matches) == 0
        out.append({"id": f"forbidden_{i}", "criterion": f"forbidden: {pat}",
                    "passes": ok,
                    "reason": "no match" if ok else f"matched {len(matches)} times"})
    return out


def _collect_grading_items(candidate: dict[str, Any]) -> list[tuple[str, dict]]:
    grade = candidate.get("grade") or {}
    cfg = grade.get("config") or {}
    typ = grade.get("type", "")
    items: list[tuple[str, dict]] = []
    rubric = cfg.get("rubric") or (cfg.get("llm_judge") or {}).get("rubric") or []
    for r in rubric:
        items.append(("rubric", r))
    assertions = cfg.get("assertions") or (cfg.get("tool_trace") or {}).get("assertions") or []
    for a in assertions:
        items.append(("assertion", a))
    regex_cfg = cfg.get("regex") or (cfg.get("tool_trace") or {}).get("regex")
    if typ == "regex":
        regex_cfg = cfg
    if regex_cfg:
        items.append(("regex_block", regex_cfg))
    return items


def grade_against_oracle(
    candidate: dict[str, Any],
    oracle_result: dict[str, Any],
) -> list[dict]:
    """Return per-criterion dicts: {id, criterion, passes, reason}."""
    trace = oracle_result.get("tool_trace") or []
    response = oracle_result.get("response_text") or ""
    items = _collect_grading_items(candidate)

    out: list[dict] = []
    for kind, payload in items:
        if kind == "assertion":
            cid = payload.get("type", "") + (
                "_" + payload.get("tool", "") if payload.get("tool") else ""
            )
            ctxt = json.dumps(payload, default=str)[:300]
            ok, reason = _eval_assertion(payload, trace)
            out.append({"id": cid, "criterion": ctxt, "passes": ok, "reason": reason})
        elif kind == "rubric":
            cid = payload.get("id", "rubric")
            ctxt = (payload.get("criterion", "") or "").strip()
            verdict = _grade_rubric_criterion(ctxt, response, trace)
            out.append({"id": cid, "criterion": ctxt,
                        "passes": bool(verdict.get("passes", False)),
                        "reason": verdict.get("reason", "")})
        elif kind == "regex_block":
            for sub in _eval_regex_block(payload, response):
                out.append(sub)
    return out
