"""Reference ingestion, evaluation, and remote execution commands."""

from __future__ import annotations

import argparse
import importlib
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=(
        "prepare-ingestion", "ingest", "plan", "evaluate", "modal",
    ))
    values = list(sys.argv[1:] if argv is None else argv)
    if not values or values[0] in {"-h", "--help"}:
        parser.print_help()
        return 0
    args = parser.parse_args(values[:1])
    rest = values[1:]
    if args.command in {"prepare-ingestion", "evaluate"}:
        if args.command == "evaluate":
            if "--confirm-paid-calls" not in rest and not {"-h", "--help"}.intersection(rest):
                parser.error("Evaluation requires --confirm-paid-calls")
            rest = [arg for arg in rest if arg != "--confirm-paid-calls"]
        from reference.evaluate import main as evaluate

        phase = "prepare" if args.command == "prepare-ingestion" else "test-only"
        return evaluate([phase, *rest])
    module = importlib.import_module({
        "ingest": "reference.ingest",
        "plan": "reference.plan",
        "modal": "reference.execution.modal",
    }[args.command])
    if args.command == "modal":
        if rest and rest[0] in {"launch", "resume"}:
            if "--confirm-paid-calls" not in rest and not {"-h", "--help"}.intersection(rest):
                parser.error("Remote execution requires --confirm-paid-calls")
            rest = [arg for arg in rest if arg != "--confirm-paid-calls"]
        previous = sys.argv
        try:
            sys.argv = [previous[0], *rest]
            return module.main()
        finally:
            sys.argv = previous
    return module.main(rest)


if __name__ == "__main__":
    raise SystemExit(main())
