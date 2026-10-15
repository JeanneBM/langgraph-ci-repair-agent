"""Minimal opt-in event emitter. Never writes prompts, answers, logs or raw tool arguments."""
from __future__ import annotations
import contextvars
import hashlib
import json
import logging
import os
from pathlib import Path
import time
import uuid

_active = contextvars.ContextVar('agent_trace', default=None)


def emit(kind, **values):
    trace = _active.get()
    if trace is not None:
        trace.event(kind, **values)


class Trace:
    def __init__(self, agent, run_id=None):
        self.agent = agent
        self.run_id = run_id or uuid.uuid4().hex
        self.status = 'success'
        self.enabled = bool(os.getenv('AGENT_TELEMETRY_FILE') or os.getenv('AGENT_TELEMETRY_STDOUT') == '1')

    def event(self, kind, **values):
        if not self.enabled:
            return
        event = dict(id=uuid.uuid4().hex, ts=time.time(), agent=self.agent,
                     run_id=self.run_id, type=kind, **values)
        line = json.dumps(event, separators=(',', ':')) + '\n'
        try:
            path = os.getenv('AGENT_TELEMETRY_FILE')
            if path:
                file = Path(path)
                file.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, line.encode())
                finally:
                    os.close(fd)
            if os.getenv('AGENT_TELEMETRY_STDOUT') == '1':
                print(json.dumps({'agent_monitor_event': event}, separators=(',', ':')), flush=True)
        except OSError:
            logging.getLogger(__name__).error('agent_telemetry_write_failed')

    def tool(self, name, arguments):
        blob = json.dumps(arguments, sort_keys=True, default=str).encode()
        self.event('tool_call', tool=name, args_hash=hashlib.sha256(blob).hexdigest() if arguments is not None else None)

    def __enter__(self):
        self.token = _active.set(self)
        self.event('run_started')
        return self

    def __exit__(self, kind, value, tb):
        if kind is not None and self.status != 'escalated':
            self.status = 'error'
            self.event('escalation', status='error', attrs={'error_type': kind.__name__})
        self.event('run_finished', status=self.status)
        _active.reset(self.token)


def tool(name, arguments):
    trace = _active.get()
    if trace is not None:
        trace.tool(name, arguments)
