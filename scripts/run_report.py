"""Run every scenario through the agent with isolated validation and check the outcomes.

Logs come either from the scenarios themselves (``--logs synthetic``) or from the
fixture jobs of a GitHub Actions run (``--logs github``), i.e. real pip and Python
output from a GitHub-hosted runner. The demo model is scripted, so this measures the
pipeline (log adapter, policy, isolated runs, guarded export), not model quality.

    python scripts/run_report.py --logs github --repo OWNER/REPO --run-id 123 \
        --isolation venv --wheelhouse .local/wheelhouse --output real-logs-report

Exit code 1 if any scenario ends differently from EXPECTED.
"""
import argparse
import json
import re
import sys
import time
import urllib.error
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ci_repair.actions_logs import api_json, fetch_job_log, normalize  # noqa: E402
from ci_repair.graph import build_graph, model_for  # noqa: E402
from ci_repair.guard import sha256_text  # noqa: E402
from ci_repair.isolation import runner_for  # noqa: E402
from ci_repair.scenarios import SCENARIOS, scenario  # noqa: E402

EXPECTED = {code: 'draft_prepared' for code in SCENARIOS} | {'S06': 'escalated'}
JOB_NAME = re.compile(r'^fixture \((S\d\d)\)$')


def github_logs(repo, run_id):
    jobs = api_json(f'/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100')['jobs']
    logs = {}
    for job in jobs:
        match = JOB_NAME.match(job['name'])
        if not match:
            continue
        for delay in (0, 5, 10, 20, 40):  # logs of a just-finished job can lag behind
            time.sleep(delay)
            try:
                raw = fetch_job_log(repo, job['id'])
                break
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
        else:
            raise SystemExit(f"log for job {job['id']} not available")
        logs[match.group(1)] = {'raw': raw, 'source': f"github:{repo}/actions/jobs/{job['id']}",
                                'url': job['html_url']}
    missing = sorted(set(SCENARIOS) - set(logs))
    if missing:
        raise SystemExit(f'no fixture job for {missing}')
    return logs


def run_one(code, runner, log=None):
    events = []
    graph = build_graph(model_for('demo'), InMemorySaver(), runner=runner, audit=events.append)
    cfg = {'configurable': {'thread_id': code}}
    initial = scenario(code)
    if log:
        initial.update(log=normalize(log['raw']), log_source=log['source'], log_sha256=sha256_text(log['raw']))
    state = graph.invoke(initial, cfg)
    before = state['status']
    if before == 'awaiting_approval':
        state = graph.invoke(Command(resume=True), cfg)
    export = [e for e in events if e.get('event') == 'authorization']
    return {
        'scenario': code,
        'log_source': initial.get('log_source', 'synthetic'),
        'log_url': (log or {}).get('url', ''),
        'log_sha256': initial.get('log_sha256', ''),
        'starting_log': initial['log'],
        'status_before_approval': before,
        'final_status': state['status'],
        'expected': EXPECTED[code],
        'attempts': state['attempts'],
        'steps': [{'path': h['path'], 'policy': h['policy_decision'], 'isolated': h['isolated']}
                  for h in state.get('history', [])],
        'feedback': state['feedback'],
        'export_decision': export[-1]['decision'] if export else None,
        'export_rules': export[-1]['matched_rules'] if export else [],
        'diff': state.get('diff', ''),
    }


def markdown(report):
    rows = ['| Scenario | Starting log | Attempts | Isolated runs | Final status | Expected |',
            '|---|---|---|---|---|---|']
    for r in report['runs']:
        source = f"[job log]({r['log_url']})" if r['log_url'] else 'synthetic'
        isolated = ', '.join(step['isolated'] for step in r['steps']) or '-'
        ok = 'yes' if r['final_status'] == r['expected'] else '**no**'
        rows.append(f"| {r['scenario']} | {source} | {r['attempts']} | {isolated} | {r['final_status']} | {ok} |")
    return '\n'.join([f"# Agent run on {report['logs']} logs ({report['isolation']} isolation)", '',
                      *rows, '', 'The demo model is scripted: this checks the pipeline, not model quality.', ''])


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--logs', choices=['synthetic', 'github'], default='synthetic')
    p.add_argument('--repo')
    p.add_argument('--run-id')
    p.add_argument('--isolation', choices=['venv', 'docker'], default='venv')
    p.add_argument('--wheelhouse')
    p.add_argument('--image', default='python:3.12-slim')
    p.add_argument('--output', default='run-report')
    args = p.parse_args()
    logs = github_logs(args.repo, args.run_id) if args.logs == 'github' else {}
    runner = runner_for(args.isolation, args.wheelhouse, args.image)
    report = {'logs': args.logs, 'isolation': args.isolation, 'model': 'scripted demo adapter',
              'runs': [run_one(code, runner, logs.get(code)) for code in SCENARIOS]}
    out = Path(args.output)
    (out / 'logs').mkdir(parents=True, exist_ok=True)
    for code, log in logs.items():
        (out / 'logs' / f'{code}.log').write_text(normalize(log['raw']) + '\n', encoding='utf-8')
    (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    (out / 'report.md').write_text(markdown(report), encoding='utf-8')
    print(markdown(report))
    failed = [r['scenario'] for r in report['runs'] if r['final_status'] != r['expected']]
    if failed:
        sys.exit(f'unexpected outcome for {failed}')


if __name__ == '__main__':
    main()
