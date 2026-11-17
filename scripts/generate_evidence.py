"""Regenerate evidence/demo-results.json from real offline runs (no hand-edited numbers).

    python scripts/generate_evidence.py          # rewrite the file
    python scripts/generate_evidence.py --check  # fail if the committed file is stale
"""
import json
import sys
from pathlib import Path
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from ci_repair.graph import build_graph, model_for
from ci_repair.guard import default_engine
from ci_repair.scenarios import SCENARIOS, scenario

TARGET = Path(__file__).resolve().parent.parent / 'evidence' / 'demo-results.json'


def build():
    runs = []
    for code in SCENARIOS:
        graph = build_graph(model_for('demo'), InMemorySaver())
        cfg = {'configurable': {'thread_id': code}}
        paused = graph.invoke(scenario(code), cfg)
        approval_needed = paused['status'] == 'awaiting_approval'
        done = graph.invoke(Command(resume=True), cfg) if approval_needed else paused
        runs.append({'scenario': code, 'status_before_approval': paused['status'],
                     'status_after_approval': done['status'] if approval_needed else 'not_applicable',
                     'attempts': done['attempts'], 'validation': done['feedback'], 'diff': done.get('diff', '')})
    engine = default_engine()
    return {'mode': 'scripted_offline_demo', 'generated_by': 'scripts/generate_evidence.py',
            'isolation': 'none (static checks only; see scripts/run_report.py for isolated runs)',
            'policy': {'id': 'langgraph-ci-repair', 'file': 'ci_repair/policies/ci_repair.yaml',
                       'sha256': engine.policy_hash},
            'live_azure_tested': False, 'remote_ci_executed': False, 'github_pr_created': False,
            'runs': runs}


if __name__ == '__main__':
    text = json.dumps(build(), indent=2) + '\n'
    if '--check' in sys.argv:
        if not TARGET.exists() or TARGET.read_text(encoding='utf-8') != text:
            sys.exit('evidence/demo-results.json is stale; run scripts/generate_evidence.py')
        print('evidence up to date')
    else:
        TARGET.write_text(text, encoding='utf-8')
        print('wrote', TARGET)
