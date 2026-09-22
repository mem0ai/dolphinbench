"""Content-free timing instrumentation for the native Hermes Honcho plugin."""
from __future__ import annotations

import functools
import importlib.abc
import importlib.machinery
import itertools
import json
import os
from pathlib import Path
import resource
import sys
import threading
import time
import uuid

HEADER = 'x-dolphinbench-diagnostic-id'
MODULES = {'plugins.memory.honcho', 'plugins.memory.honcho.session',
           'agent.memory_manager', 'httpx'}


def resource_snapshot() -> dict:
    result = {'process_cpu_seconds': time.process_time(),
              'max_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
    for name in ('cpu.stat', 'cpu.pressure', 'memory.current', 'memory.events'):
        try:
            result[name] = Path('/sys/fs/cgroup', name).read_text().strip()
        except OSError:
            pass
    return result


class Recorder:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self.started = time.monotonic()
        self.run_id = uuid.uuid4().hex
        self.ids = itertools.count(1)
        self.lock = threading.Lock()

    def emit(self, event: str, **values) -> None:
        # Logging failures must not become retrieval failures.
        try:
            row = dict(event=event, unix_seconds=time.time(),
                       elapsed_seconds=time.monotonic() - self.started,
                       run_id=self.run_id, pid=os.getpid(),
                       thread=threading.current_thread().name, **values)
            line = (json.dumps(row, separators=(',', ':')) + '\n').encode()
            with self.lock:
                os.write(self.fd, line)
        except Exception:
            pass


def _state(provider) -> dict:
    cfg = getattr(provider, '_config', None)
    return {key: getattr(provider, attr, None) for key, attr in {
        'turn': '_turn_count', 'session_initialized': '_session_initialized',
        'recall_mode': '_recall_mode', 'injection_frequency': '_injection_frequency',
        'base_wait_seconds': '_FIRST_TURN_BASE_TIMEOUT',
        'dialectic_wait_seconds': '_FIRST_TURN_DIALECTIC_CAP',
    }.items()} | {'request_timeout_seconds': getattr(cfg, 'timeout', None)}


def wrap_method(cls, name: str, recorder: Recorder, *, provider=False) -> None:
    original = getattr(cls, name)
    @functools.wraps(original)
    def wrapped(self, *args, **kwargs):
        call_id = next(recorder.ids)
        start = time.monotonic()
        values = _state(self) if provider else {}
        if name == '_prefetch_provider' and args:
            values['provider'] = getattr(args[0], 'name', None)
            values['outer_timeout_seconds'] = getattr(self, '_external_prefetch_timeout', None)
        recorder.emit(name + '.start', call_id=call_id, **values)
        try:
            result = original(self, *args, **kwargs)
        except BaseException as exc:
            recorder.emit(name + '.error', call_id=call_id,
                          duration_seconds=time.monotonic() - start,
                          error_type=type(exc).__name__)
            raise
        values = _state(self) if provider else {}
        if isinstance(result, str):
            values['returned_chars'] = len(result)
        elif isinstance(result, dict):
            values['field_chars'] = {k: len(v) for k, v in result.items() if isinstance(v, str)}
        if name == 'set_context_result' and len(args) > 1 and isinstance(args[1], dict):
            values['field_chars'] = {k: len(v) for k, v in args[1].items() if isinstance(v, str)}
        recorder.emit(name + '.end', call_id=call_id,
                      duration_seconds=time.monotonic() - start, **values)
        if name in ('prefetch', '_prefetch_provider'):
            recorder.emit('resources', **resource_snapshot())
        return result
    setattr(cls, name, wrapped)


def instrument_httpx(module, recorder: Recorder) -> None:
    original = module.Client.send
    @functools.wraps(original)
    def send(client, request, *args, **kwargs):
        if not request.url.path.startswith('/v3/workspaces'):
            return original(client, request, *args, **kwargs)
        request_id = f'{recorder.run_id}-{next(recorder.ids)}'
        request.headers[HEADER] = request_id
        start = time.monotonic()
        parts = request.url.path.split('/')
        for marker in ('workspaces', 'peers', 'sessions'):
            for i, part in enumerate(parts[:-1]):
                if part == marker:
                    parts[i + 1] = '{id}'
        recorder.emit('http.start', request_id=request_id, method=request.method,
                      route='/'.join(parts), timeout=request.extensions.get('timeout'))
        prior_trace = request.extensions.get('trace')
        def trace(name, info):
            fields = {}
            if isinstance(info.get('exception'), BaseException):
                fields['error_type'] = type(info['exception']).__name__
            recorder.emit('http.transport', request_id=request_id, stage=name, **fields)
            if prior_trace is not None:
                prior_trace(name, info)
        request.extensions['trace'] = trace
        try:
            response = original(client, request, *args, **kwargs)
            recorder.emit('http.end', request_id=request_id, status=response.status_code,
                          duration_seconds=time.monotonic() - start,
                          server_request_id=response.headers.get('x-request-id'),
                          server_timing=response.headers.get('server-timing'))
            return response
        except BaseException as exc:
            recorder.emit('http.error', request_id=request_id,
                          duration_seconds=time.monotonic() - start,
                          error_type=type(exc).__name__)
            raise
        finally:
            if prior_trace is None:
                request.extensions.pop('trace', None)
            else:
                request.extensions['trace'] = prior_trace
    module.Client.send = send


def instrument_module(module, recorder: Recorder) -> None:
    if module.__name__ == 'plugins.memory.honcho':
        for name in ('initialize', '_do_session_init', 'prefetch', '_run_dialectic_depth'):
            wrap_method(module.HonchoMemoryProvider, name, recorder, provider=True)
    elif module.__name__ == 'plugins.memory.honcho.session':
        for name in ('get_prefetch_context', 'set_context_result', 'pop_context_result'):
            wrap_method(module.HonchoSessionManager, name, recorder)
    elif module.__name__ == 'agent.memory_manager':
        for name in ('prefetch_all', '_prefetch_provider'):
            wrap_method(module.MemoryManager, name, recorder)
    elif module.__name__ == 'httpx':
        instrument_httpx(module, recorder)
    recorder.emit('instrumented', module=module.__name__)


class InstrumentingFinder(importlib.abc.MetaPathFinder):
    """Instrument after normal imports, without preloading Hermes or its profile."""
    def __init__(self, recorder: Recorder):
        self.recorder = recorder

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in MODULES:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None or spec.loader is None:
            return spec
        original = spec.loader
        recorder = self.recorder
        class Loader(importlib.abc.Loader):
            def create_module(self, spec):
                return original.create_module(spec)
            def exec_module(self, module):
                original.exec_module(module)
                instrument_module(module, recorder)
        spec.loader = Loader()
        return spec


def install(path: Path) -> Recorder:
    recorder = Recorder(path)
    recorder.emit('process.start', resources=resource_snapshot(),
                  worker={key: os.environ.get(key) for key in
                          ('MODAL_TASK_ID', 'MODAL_REGION', 'MODAL_CLOUD_PROVIDER')})
    for name in MODULES:
        if name in sys.modules:
            instrument_module(sys.modules[name], recorder)
    sys.meta_path.insert(0, InstrumentingFinder(recorder))
    return recorder
