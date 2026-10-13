import argparse
import json
from pathlib import Path
from uuid import uuid4
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from .graph import build_graph, model_for
from .scenarios import scenario


def main():
    p = argparse.ArgumentParser(description='Bounded CI repair PoC')
    p.add_argument('action', choices=['run', 'approve', 'reject', 'status'])
    p.add_argument('--scenario', choices=['S01', 'S02', 'S03', 'S04'], default='S01')
    p.add_argument('--mode', choices=['demo', 'azure'], default='demo')
    p.add_argument('--thread')
    p.add_argument('--output', default='artifacts')
    args = p.parse_args()
    if args.action != 'run' and not args.thread:
        p.error('--thread is required for resume/status')
    thread = args.thread or str(uuid4())
    # IDs are also directory names; reject traversal and reserved characters.
    if not thread or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in thread):
        p.error('Use letters, numbers, underscores or hyphens in --thread')
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(root / 'checkpoints.sqlite')) as saver:
        # Resume/export does not need access to the model.
        graph = build_graph(model_for(args.mode if args.action == 'run' else 'demo'), saver)
        cfg = {'configurable': {'thread_id': thread}}
        snapshot = graph.get_state(cfg)
        if args.action == 'run':
            if snapshot.values:
                p.error('Thread already exists; use a new ID or resume it')
            graph.invoke(scenario(args.scenario), cfg)
        elif args.action in ('approve', 'reject'):
            if not any(t.interrupts for t in snapshot.tasks):
                p.error('Thread has no pending approval')
            graph.invoke(Command(resume=args.action == 'approve'), cfg)
        state = graph.get_state(cfg).values
        if not state:
            p.error('Unknown thread')
        directory = root / thread
        directory.mkdir(exist_ok=True)
        (directory / 'evidence.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
        if state['status'] == 'draft_prepared':
            (directory / 'repair.patch').write_text(state['diff'], encoding='utf-8')
            (directory / 'draft-pr.md').write_text(state['draft'], encoding='utf-8')
        print(json.dumps({'thread': thread, 'status': state['status'],
                          'attempts': state['attempts'], 'output': str(directory)}, indent=2))

if __name__ == '__main__':
    main()
