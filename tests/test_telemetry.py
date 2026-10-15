import json
from langgraph.checkpoint.memory import InMemorySaver
from ci_repair.graph import build_graph, model_for
from ci_repair.scenarios import scenario
from ci_repair.telemetry import Trace


def test_observation_does_not_bypass_human_approval(tmp_path,monkeypatch):
    file=tmp_path/'events';monkeypatch.setenv('AGENT_TELEMETRY_FILE',str(file))
    graph=build_graph(model_for('demo'),InMemorySaver())
    config={'configurable':{'thread_id':'observed'}}
    with Trace('ci-repair'):
        graph.invoke(scenario('S01'),config)
    assert graph.get_state(config).values['status']=='awaiting_approval'
    events=[json.loads(line) for line in file.read_text().splitlines()]
    assert any(e.get('tool')=='policy_check' for e in events)
    assert any(e['type']=='verification' and e['status']=='ok' for e in events)
    assert not any(e.get('tool')=='draft_export' for e in events)
