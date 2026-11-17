import argparse
import json
from pathlib import Path
from uuid import uuid4
from agent_policy_guard import JsonlAudit
from dotenv import load_dotenv
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from .actions_logs import fetch_job_log, normalize, parse_job_reference
from .graph import build_graph, model_for
from .guard import sha256_text
from .isolation import runner_for
from .scenarios import SCENARIOS, scenario
from .telemetry import Trace


def main():
    p = argparse.ArgumentParser(description='Bounded CI repair PoC')
    p.add_argument('action', choices=['run', 'approve', 'reject', 'status'])
    p.add_argument('--scenario', choices=SCENARIOS, default='S01')
    p.add_argument('--mode', choices=['demo', 'azure'], default='demo')
    p.add_argument('--thread')
    p.add_argument('--output', default='artifacts')
    source = p.add_mutually_exclusive_group()
    source.add_argument('--log-file', help='Real failure log (raw GitHub Actions job log or plain text)')
    source.add_argument('--github-job', metavar='OWNER/REPO:JOB_ID',
                        help='Download a job log read-only (token from GITHUB_TOKEN or GH_TOKEN)')
    p.add_argument('--isolation', choices=['none', 'venv', 'docker'], default='none',
                   help='Run each candidate\'s CI commands in isolation before approval')
    p.add_argument('--wheelhouse', help='Pre-fetched wheels for isolated installs (required for docker)')
    p.add_argument('--image', default='python:3.12-slim', help='Container image for --isolation docker')
    args = p.parse_args()
    load_dotenv(Path.cwd() / '.env', override=False)
    if args.action != 'run' and not args.thread:
        p.error('--thread is required for resume/status')
    thread = args.thread or str(uuid4())
    # IDs are also directory names; reject traversal and reserved characters.
    if not thread or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in thread):
        p.error('Use letters, numbers, underscores or hyphens in --thread')
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    directory = root / thread
    try:  # isolation only matters while proposals are validated, i.e. during `run`
        runner = runner_for(args.isolation, args.wheelhouse, args.image) if args.action == 'run' else None
    except ValueError as exc:
        p.error(str(exc))
    with SqliteSaver.from_conn_string(str(root / 'checkpoints.sqlite')) as saver:
        # Resume/export does not need access to the model.
        graph = build_graph(model_for(args.mode if args.action == 'run' else 'demo'), saver, runner=runner,
                            audit=JsonlAudit(directory / 'policy-audit.jsonl'))
        cfg = {'configurable': {'thread_id': thread}}
        snapshot = graph.get_state(cfg)
        if args.action == 'run':
            if snapshot.values:
                p.error('Thread already exists; use a new ID or resume it')
            initial = scenario(args.scenario)
            if args.log_file or args.github_job:
                if args.log_file:
                    raw = Path(args.log_file).read_text(encoding='utf-8', errors='replace')
                    initial['log_source'] = 'file:' + Path(args.log_file).name
                else:
                    try:
                        repo, job_id = parse_job_reference(args.github_job)
                    except ValueError as exc:
                        p.error(str(exc))
                    try:
                        raw = fetch_job_log(repo, job_id)
                    except (OSError, ValueError) as exc:  # URLError/HTTPError are OSErrors
                        p.error(f'Could not download the job log: {exc}')
                    initial['log_source'] = f'github:{repo}/actions/jobs/{job_id}'
                initial.update(log=normalize(raw), log_sha256=sha256_text(raw))
            with Trace('ci-repair', thread + '-run') as trace:
                graph.invoke(initial, cfg)
                outcome = graph.get_state(cfg).values
                if outcome.get('status') in ('awaiting_approval', 'escalated', 'blocked'):
                    trace.status = 'escalated'
                    trace.event('escalation', status=outcome['status'])
        elif args.action in ('approve', 'reject'):
            if not any(t.interrupts for t in snapshot.tasks):
                p.error('Thread has no pending approval')
            with Trace('ci-repair') as trace:
                graph.invoke(Command(resume=args.action == 'approve'), cfg)
                if graph.get_state(cfg).values.get('status') == 'export_blocked':
                    trace.status = 'escalated'
                    trace.event('escalation', status='export_blocked')
        state = graph.get_state(cfg).values
        if not state:
            p.error('Unknown thread')
        directory.mkdir(exist_ok=True)
        (directory / 'evidence.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
        if state['status'] == 'draft_prepared':
            (directory / 'repair.patch').write_text(state['diff'], encoding='utf-8')
            (directory / 'draft-pr.md').write_text(state['draft'], encoding='utf-8')
        print(json.dumps({'thread': thread, 'status': state['status'],
                          'attempts': state['attempts'], 'output': str(directory)}, indent=2))

if __name__ == '__main__':
    main()
