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
from ci_repair.scenarios import scenario

TARGET = Path(__file__).resolve().parent.parent / 'evidence' / 'demo-results.json'


def build():
    runs = []
    for code in ['S01', 'S02', 'S03', 'S04']:
        graph = build_graph(model_for('demo'), InMemorySaver())
        cfg = {'configurable': {'thread_id': code}}
        paused = graph.invoke(scenario(code), cfg)
        done = graph.invoke(Command(resume=True), cfg)
        runs.append({'scenario': code, 'status_before_approval': paused['status'],
                     'status_after_approval': done['status'], 'attempts': done['attempts'],
                     'validation': done['feedback'], 'diff': done['diff']})
    return {'mode': 'scripted_offline_demo', 'generated_by': 'scripts/generate_evidence.py',
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
