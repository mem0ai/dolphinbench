#!/usr/bin/env python3
"""ONE oracle exam run, in an isolated process -- the unit of the parallel gate.

The mock server locks to one persona per process and reads its state/log file
paths from env at import, so true parallelism = one process per shot, each with
its own state/log env paths set by the parent.

Usage: _gate_shot.py <candidate.json> <persona> <with_memory:0|1> [checkpoint]
Prints one JSON line: {"passed": bool|null, "ncalls": int, "error": str|null}
"""
import os, sys, json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

cand_path, persona, with_mem = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
checkpoint = sys.argv[4] if len(sys.argv) > 4 else None
if checkpoint:
    # oracle_preview is an existing subprocess boundary. The oracle resolves
    # this bridge while its public Python API also accepts checkpoint directly.
    os.environ["DOLPHINBENCH_CHECKPOINT"] = checkpoint
else:
    os.environ.pop("DOLPHINBENCH_CHECKPOINT", None)
candidate = json.load(open(cand_path))
out = {
    "passed": None,
    "ncalls": 0,
    "error": None,
    "tool_calls": [],
    "response_text": "",
    "oracle_errors": [],
    "grade": None,
}
try:
    from harness.mining.preview import oracle_preview, prod_grade_result
    settings = (candidate.get("_oracle_input") or {}).get("execution_settings") or {}
    r = oracle_preview(candidate, persona, with_memory=with_mem,
                       state_override=candidate.get("mock_state"),
                       **({"oracle_model": settings["model"]} if settings else {}))
    out["ncalls"] = len(r.get("tool_trace") or [])
    out["tool_calls"] = r.get("tool_calls") or []
    out["response_text"] = r.get("response_text") or ""
    out["oracle_errors"] = r.get("errors") or []
    out["agent_input"] = r.get("agent_input")
    out["messages"] = r.get("messages") or []
    if "provider_continuation" in r:
        out["provider_continuation"] = r["provider_continuation"]
    out["tool_results"] = r.get("tool_results") or []
    for key in ("usage", "latency_seconds", "model", "turn_count", "turn_ceiling_hit"):
        out[key] = r.get(key)
    out["grade"] = prod_grade_result(candidate, r)
    out["passed"] = (
        bool(out["grade"].get("passed"))
        if isinstance(out["grade"], dict)
        else None
    )
except Exception as ex:
    out["error"] = f"{type(ex).__name__}: {ex}"[:200]
print("\n" + json.dumps(out), flush=True)
