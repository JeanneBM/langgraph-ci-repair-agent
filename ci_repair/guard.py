"""Host-side binding to agent-policy-guard.

The model proposes; this module builds the facts from host-owned data, a versioned
YAML policy decides, and the export runs only through a GuardedExecutor. Nothing
in here reads a fact from model output.
"""
import hashlib
import json
from functools import lru_cache
from pathlib import Path
from agent_policy_guard import Action, AuthorizationDenied, Context, Decision, GuardedExecutor, PolicyEngine

POLICY_FILE = Path(__file__).with_name('policies') / 'ci_repair.yaml'
ACTOR = 'ci-repair-agent'
TARGET = 'synthetic/ci-fixture'

__all__ = ['AuthorizationDenied', 'Decision', 'accept_decision', 'audit_record', 'default_engine',
           'export_context', 'export_guard', 'proposal_facts', 'sha256_text', 'snapshot_sha256']


@lru_cache(maxsize=1)
def default_engine():
    return PolicyEngine.from_yaml(POLICY_FILE)


def sha256_text(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def snapshot_sha256(files):
    return sha256_text(json.dumps(files, sort_keys=True, separators=(',', ':')))


def proposal_facts(files, path, content, authorized):
    """Facts about one proposal, derived from the snapshot and the repair rules only."""
    return {
        'path_in_snapshot': path in files,
        'changes_file': path in files and files[path] != content,
        'touches_tests': path.startswith('tests/'),
        'rule_authorized': (path, content) in authorized,
    }


def accept_decision(engine, path, content, facts):
    action = Action(type='ci.accept_proposal', target=TARGET,
                    parameters={'path': path, 'content_sha256': sha256_text(content)})
    context = Context(actor=ACTOR, environment='agent', facts=facts)
    return engine.evaluate(action, context)


def audit_record(event, result):
    """Metadata-only audit event: no proposal text, log or file content."""
    return {'event': event, 'policy_id': result.policy_id, 'policy_version': result.policy_version,
            'policy_hash': result.policy_hash, 'request_hash': result.request_hash,
            'decision': result.decision.value, 'matched_rules': list(result.matched_rules)}


def export_context(state, static_ok):
    """Recompute export facts at export time instead of trusting earlier flags."""
    files, working = state['files'], state.get('working', state['files'])
    changed = [p for p in working if working[p] != files.get(p)]
    isolation = state.get('isolation')
    if isolation is None:
        isolated, environment = 'not_run', 'static-demo'
    elif isolation.get('snapshot_sha256') != snapshot_sha256(working):
        isolated, environment = 'stale', 'lab'
    else:
        isolated, environment = isolation['status'], 'lab'
    history = state.get('history', [])
    facts = {
        'human_approved': state.get('status') == 'approved',
        'static_validation_passed': bool(static_ok),
        'isolated_check': isolated,
        'all_steps_allowed': bool(history) and all(h.get('policy_decision') == 'allow' for h in history),
        'touches_tests': any(p.startswith('tests/') for p in changed),
    }
    action = Action(type='ci.export_patch', target=TARGET,
                    parameters={'diff_sha256': sha256_text(state.get('diff', '')),
                                'snapshot_sha256': snapshot_sha256(working)})
    return action, Context(actor=ACTOR, environment=environment, facts=facts)


def export_guard(engine, executor, audit):
    return GuardedExecutor(engine, action_type='ci.export_patch', executor=executor, audit=audit)
