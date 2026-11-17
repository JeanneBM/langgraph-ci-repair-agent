"""Policy-as-code: the YAML policy, host-built facts and the guarded export."""
import json
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from ci_repair.graph import Proposal, build_graph, evaluate_proposal, model_for
from ci_repair.guard import (Decision, default_engine, export_context, proposal_facts, snapshot_sha256)
from ci_repair.isolation import IsolatedResult
from ci_repair.scenarios import WORKFLOW, scenario


class ScriptedRunner:
    """Stands in for an isolated runner: returns prepared results in order."""
    name = 'scripted'

    def __init__(self, *results, sha=None):
        self.results, self.sha, self.calls = list(results), sha, []

    def run(self, files):
        self.calls.append(files)
        status, log = self.results.pop(0)
        return IsolatedResult(status, self.name, self.sha or snapshot_sha256(files), log,
                              '' if status == 'passed' else 'exit code 1')


def approved_state(**overrides):
    s = scenario('S01')
    working = {**s['files'], WORKFLOW: s['files'][WORKFLOW].replace('requirement.txt', 'requirements.txt')}
    state = {**s, 'working': working, 'status': 'approved', 'diff': 'd',
             'history': [{'attempt': 1, 'path': WORKFLOW, 'policy_decision': 'allow'}]}
    return {**state, **overrides}


def test_policy_file_is_versioned_and_denies_unknown_actions():
    engine = default_engine()
    assert len(engine.policy_hash) == 64
    from agent_policy_guard import Action, Context
    result = engine.evaluate(Action(type='ci.delete_repository', target='x'), Context(actor='ci-repair-agent',
                                                                                      environment='lab'))
    assert result.decision is Decision.DENY and result.policy_version == '1.0.0'


def test_test_files_stay_protected_even_when_a_rule_would_produce_the_change():
    facts = {'path_in_snapshot': True, 'changes_file': True, 'touches_tests': True, 'rule_authorized': True}
    from ci_repair.guard import accept_decision
    result = accept_decision(default_engine(), 'tests/test_app.py', 'x', facts)
    assert result.decision is Decision.DENY and 'protected-tests' in result.matched_rules


def test_proposal_facts_come_from_the_snapshot_not_the_model():
    files = scenario('S01')['files']
    facts = proposal_facts(files, 'requirements.txt', files['requirements.txt'], [])
    assert facts == {'path_in_snapshot': True, 'changes_file': False, 'touches_tests': False,
                     'rule_authorized': False}


def test_export_without_approval_needs_a_human():
    action, context = export_context(approved_state(status='awaiting_approval'), static_ok=True)
    assert default_engine().evaluate(action, context).decision is Decision.REQUIRE_HUMAN


def test_static_demo_export_is_allowed_only_without_an_isolated_run():
    action, context = export_context(approved_state(), static_ok=True)
    result = default_engine().evaluate(action, context)
    assert context.environment == 'static-demo' and context.facts['isolated_check'] == 'not_run'
    assert result.decision is Decision.ALLOW and result.matched_rules == ('export-static-demo',)


def test_export_denied_when_isolated_evidence_belongs_to_another_snapshot():
    stale = {'status': 'passed', 'backend': 'venv', 'snapshot_sha256': 'f' * 64}
    action, context = export_context(approved_state(isolation=stale), static_ok=True)
    assert context.facts['isolated_check'] == 'stale'
    assert default_engine().evaluate(action, context).decision is Decision.DENY


def test_export_denied_when_a_test_file_changed():
    state = approved_state()
    state['working'] = {**state['working'], 'tests/test_app.py': 'def test_sum():\n    pass\n'}
    action, context = export_context(state, static_ok=True)
    assert default_engine().evaluate(action, context).decision is Decision.DENY


def test_gate_and_export_write_metadata_only_audit_events():
    events = []
    g = build_graph(model_for('demo'), InMemorySaver(), audit=events.append)
    cfg = {'configurable': {'thread_id': 'audit'}}
    g.invoke(scenario('S01'), cfg)
    g.invoke(Command(resume=True), cfg)
    kinds = [e['event'] for e in events]
    assert kinds == ['proposal_decision', 'authorization', 'execution_returned']
    assert all(e['policy_id'] == 'langgraph-ci-repair' and len(e['policy_hash']) == 64 for e in events)
    text = json.dumps(events)
    assert 'requirements.txt' not in text and 'requirement.txt' not in text  # no file content or paths


def test_stale_isolated_evidence_blocks_export_inside_the_graph():
    runner = ScriptedRunner(('passed', ''), sha='0' * 64)  # claims a pass for a different snapshot
    g = build_graph(model_for('demo'), InMemorySaver(), runner=runner)
    cfg = {'configurable': {'thread_id': 'stale'}}
    assert g.invoke(scenario('S01'), cfg)['status'] == 'awaiting_approval'
    result = g.invoke(Command(resume=True), cfg)
    assert result['status'] == 'export_blocked'
    assert result['feedback'].startswith('deny:')
    assert 'draft' not in result


def test_policy_reason_matches_the_rule_that_decided():
    s = scenario('S01')
    result = evaluate_proposal(s['files'], Proposal(diagnosis='x', path='../escape', content='x'), s['log'])
    assert result.reason == 'Path outside permitted scope.'
