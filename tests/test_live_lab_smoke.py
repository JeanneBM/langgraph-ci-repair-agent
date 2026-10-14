"""Test the live graph smoke adapter with process/evidence substitutes."""

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "problem,expected",
    [
        (None, "PASS"),
        ("provider", "INCONCLUSIVE"),
        ("policy", "FAIL"),
        ("rejection_export", "FAIL"),
    ],
)
def test_graph_smoke_requires_restart_approval_and_no_rejected_export(
    tmp_path, monkeypatch, problem, expected
):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "graph_smoke_adapter", scripts / "cloud_validation.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    adapter = module.CloudLab(tmp_path, "graph", "rg-demo", "graph")
    commands = []

    def execute(command, root, directory, **kwargs):
        commands.append(command)
        thread = command[command.index("--thread") + 1]
        folder = Path(command[command.index("--output") + 1]) / thread
        folder.mkdir(parents=True, exist_ok=True)
        action = command[3]
        if action == "run":
            data = {"status": "awaiting_approval"}
            if problem in {"provider", "policy"}:
                data = {
                    "status": "escalated",
                    "feedback": "Model response failed or did not match the schema."
                    if problem == "provider"
                    else "Policy refused",
                }
        elif action == "approve":
            data = {"status": "draft_prepared", "diff": "patch", "draft": "draft"}
            (folder / "repair.patch").write_text("patch")
            (folder / "draft-pr.md").write_text("draft")
        else:
            data = {"status": "rejected"}
            if problem == "rejection_export":
                (folder / "repair.patch").write_text("unexpected")
        (folder / "evidence.json").write_text(json.dumps(data))

    monkeypatch.setattr(module, "execute", execute)
    assert adapter.graph_smoke(tmp_path, {}).verdict == expected
    if problem is None:
        assert [cmd[3] for cmd in commands] == ["run", "approve", "run", "reject"]
        assert len(list(tmp_path.rglob("approval-gate.json"))) == 2
