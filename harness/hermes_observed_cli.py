"""Run the original Hermes CLI with benchmark-only observation enabled."""
import os
import functools
import json
import importlib.abc
import importlib.machinery
from pathlib import Path
import runpy
import sys

from harness.memory_diagnostics import install


def observe_turns(agent_class, path: Path):
    """Record the agent's completion status without interpreting its prose."""
    original = agent_class.run_conversation

    @functools.wraps(original)
    def observed(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if isinstance(result, dict):
            status = {key: result.get(key) for key in ("completed", "partial", "error")}
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(status), encoding="utf-8")
            temporary.replace(path)
        return result

    agent_class.run_conversation = observed


class TurnStatusFinder(importlib.abc.MetaPathFinder):
    def __init__(self, status_path: Path):
        self.status_path = status_path

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "run_agent":
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return None
        original = spec.loader
        status_path = self.status_path

        class Loader(importlib.abc.Loader):
            def create_module(self, spec):
                return original.create_module(spec)

            def exec_module(self, module):
                original.exec_module(module)
                observe_turns(module.AIAgent, status_path)

        spec.loader = Loader()
        return spec


def main():
    recorder = None
    if os.environ.get('DOLPHINBENCH_MEMORY_DIAGNOSTICS_PATH'):
        recorder = install(Path(os.environ['DOLPHINBENCH_MEMORY_DIAGNOSTICS_PATH']))
    if os.environ.get('DOLPHINBENCH_SUBMISSION_TRACE_DIR'):
        from harness.submission_capture import install as install_submission
        install_submission(Path(os.environ['DOLPHINBENCH_SUBMISSION_TRACE_DIR']))
    binary = sys.argv[1]
    if status_path := os.environ.get('DOLPHINBENCH_TURN_STATUS_PATH'):
        # Let the CLI select its profile before importing the agent.
        sys.meta_path.insert(0, TurnStatusFinder(Path(status_path)))
    sys.argv = sys.argv[1:]
    try:
        runpy.run_path(binary, run_name='__main__')
    finally:
        if recorder is not None:
            recorder.emit('process.exit')


if __name__ == '__main__':
    main()
