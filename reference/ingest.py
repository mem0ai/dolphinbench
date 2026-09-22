"""Run a prepared Hermes ingestion and preserve completed-session checkpoints."""

from __future__ import annotations

import os
import json
import runpy
import sys
import time
from pathlib import Path
from uuid import uuid4


def wait_for_checkpoint(control: Path, payload: object, timeout: float = 600) -> None:
    if not isinstance(payload, dict) or not (calls := payload.get("seed_calls")):
        return
    # Failure records must never advertise an interrupted turn as a safe save.
    if calls[-1].get("seed_delivery_state") != "delivered":
        return
    request = control / "request"
    try:
        token = request.read_text()
    except FileNotFoundError:
        return
    ready = control / "ready"
    ready.write_text(token)
    deadline = time.monotonic() + timeout
    while True:
        try:
            if request.read_text() != token:
                break
        except FileNotFoundError:
            break
        if (control / "error").exists():
            raise RuntimeError("checkpoint failed; ingestion stopped before the next source")
        if time.monotonic() >= deadline:
            raise RuntimeError("checkpoint acknowledgment timed out; ingestion stopped")
        time.sleep(0.1)
    ready.unlink(missing_ok=True)


def run(runner: str, control: str, *, handoff_seconds: float | None = None) -> int:
    if "--seed-only" not in sys.argv or "--skip-seeding" in sys.argv:
        raise RuntimeError("checkpoint wrapper requires ingestion-only execution")
    # The saved benchmark runner allows this drain; CLI cleanup must outlive it.
    os.environ["HERMES_MEMORY_SYNC_DRAIN_TIMEOUT_SECONDS"] = "6000"
    os.environ["HERMES_EXIT_WATCHDOG_S"] = "6120"
    sys.argv[0] = runner
    namespace = runpy.run_path(runner, run_name="_saved_ingestion_runner")
    main = namespace["main"]
    original_write = main.__globals__["_atomic_write_json"]
    deadline = None if handoff_seconds is None else time.monotonic() + handoff_seconds

    def save(path: Path, payload: object) -> None:
        original_write(path, payload)
        if deadline is not None and time.monotonic() >= deadline and isinstance(payload, dict):
            calls = payload.get("seed_calls", [])
            if calls and calls[-1].get("seed_delivery_state") == "delivered":
                directory = Path(control)
                request = directory / "request"
                if not request.exists():
                    request.write_text(uuid4().hex)
                wait_for_checkpoint(directory, payload)
                (directory / "handoff.json").write_text(json.dumps({
                    "result_path": str(path), "completed": len(calls),
                }))
                raise SystemExit(0)
        wait_for_checkpoint(Path(control), payload)

    main.__globals__["_atomic_write_json"] = save
    return main()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--confirm-paid-calls", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm_paid_calls:
        parser.error("Ingestion requires --confirm-paid-calls")
    from reference.evaluate import launch_matrix

    return launch_matrix(
        args.manifest, args.concurrency, (), resume=args.resume, seed_only=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
