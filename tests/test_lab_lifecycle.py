"""Offline lifecycle tests; Azure operations are replaced before any command runs."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/lab_support.py"
spec = importlib.util.spec_from_file_location("lab_support_under_test", SCRIPT)
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


class Azure:
    def __init__(self):
        self.group = None
        self.calls = []
        self.keep_group = False

    def __call__(self, *args, subscription=None):
        self.calls.append((args, subscription))
        if args[:2] == ("account", "show"):
            return {"id": "sub"}
        if args[:2] == ("group", "exists"):
            return self.group is not None
        if args[:2] == ("group", "show"):
            return self.group
        if args[:2] == ("group", "create"):
            group = args[args.index("-n") + 1]
            generation = next(
                x.split("=", 1)[1] for x in args if x.startswith("lab-generation=")
            )
            self.group = {
                "id": f"/subscriptions/sub/resourceGroups/{group}",
                "tags": {"lab-generation": generation},
            }
        elif args[:2] == ("group", "delete") and not self.keep_group:
            self.group = None
        return {}


@pytest.fixture
def azure(monkeypatch):
    fake = Azure()
    monkeypatch.setattr(lab, "az", fake)
    return fake


def setup(tmp_path):
    return lab.prepare(
        tmp_path, "demo", "sub", "rg-demo", {"location": "swedencentral"}
    )


def test_state_precedes_first_cloud_mutation_and_resume_keeps_names(tmp_path, azure):
    native = azure.__call__

    def checked(*args, **kwargs):
        if args[:2] == ("group", "create"):
            assert (tmp_path / ".local/lab.json").exists()
        return native(*args, **kwargs)

    lab.az = checked
    path, state = setup(tmp_path)
    _, resumed = setup(tmp_path)
    assert state["accountName"] == resumed["accountName"]
    assert json.loads(path.read_text())["generation"] == state["generation"]


def test_recreate_after_delete_uses_new_names_and_retains_prior_state(tmp_path, azure):
    _, first = setup(tmp_path)
    lab.delete(tmp_path, "rg-demo")
    _, second = setup(tmp_path)
    assert first["accountName"] != second["accountName"]
    assert (tmp_path / f".local/retired/{first['generation']}.json").exists()


def test_unowned_existing_group_is_not_adopted(tmp_path, azure):
    azure.group = {"id": "unrelated", "tags": {}}
    with pytest.raises(lab.LabError, match="already exists"):
        setup(tmp_path)
    assert not any(args[:2] == ("group", "create") for args, _ in azure.calls)


@pytest.mark.parametrize("change", ["subscription", "generation"])
def test_wrong_subscription_or_replaced_group_blocks_delete(tmp_path, azure, change):
    path, state = setup(tmp_path)
    if change == "subscription":
        state["subscriptionId"] = "other-sub"
        lab.save(path, state)
    else:
        azure.group["tags"]["lab-generation"] = "replacement"
    with pytest.raises(lab.LabError, match="ownership"):
        lab.delete(tmp_path, "rg-demo")
    assert not any(args[:2] == ("group", "delete") for args, _ in azure.calls)


def test_bad_confirmation_and_incomplete_deletion_cannot_succeed(tmp_path, azure):
    setup(tmp_path)
    with pytest.raises(lab.LabError, match="Confirmation"):
        lab.delete(tmp_path, "wrong")
    azure.keep_group = True
    with pytest.raises(lab.LabError, match="incomplete"):
        lab.delete(tmp_path, "rg-demo")
    assert not lab.load(tmp_path)[1].get("deleted")


def test_run_uses_recorded_configuration_and_does_not_spawn_for_partial_setup(
    tmp_path, azure, monkeypatch
):
    path, state = setup(tmp_path)
    with pytest.raises(lab.LabError, match="incomplete"):
        lab.child(tmp_path, state, ["chat.py"])
    state.update(ready=True, environment={"PROJECT_ENDPOINT": "recorded"})
    lab.save(path, state)
    monkeypatch.setenv("PROJECT_ENDPOINT", "unrelated")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "secret")
    calls = []
    monkeypatch.setattr(
        lab.subprocess, "call", lambda cmd, **kwargs: calls.append((cmd, kwargs)) or 0
    )
    assert lab.child(tmp_path, state, ["chat.py"]) == 0
    assert calls[0][1]["env"]["PROJECT_ENDPOINT"] == "recorded"
    assert calls[0][1]["env"]["AZURE_OPENAI_API_KEY"] == ""


def test_changed_parameters_refuse_existing_lab(tmp_path, azure):
    setup(tmp_path)
    with pytest.raises(lab.LabError, match="different setup"):
        lab.prepare(tmp_path, "demo", "sub", "rg-demo", {"location": "different"})
