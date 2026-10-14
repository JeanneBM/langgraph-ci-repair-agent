"""Exercise reporting/control flow with a fake lab; never contact Azure."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location(
    "validation_test_contract", SCRIPTS / "lab_validation.py"
)
validation = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = validation
spec.loader.exec_module(validation)
Outcome = validation.Outcome


class Lab:
    repository = "one-repository"

    def __init__(self):
        self.generation = 0
        self.deleted = []
        self.fail_setup = False
        self.fail_cleanup = False
        self.reuse_name = False
        self.smoke_verdict = "PASS"
        self.unavailable = False
        self.interrupt = False

    def preflight(self, subscription, confirm, directory):
        return Outcome("PASS", "isolated")

    def setup(self, subscription, directory):
        self.generation += 1
        if self.fail_setup:
            raise RuntimeError("REMOTE_SECRET")
        return Outcome("PASS", "created")

    def identity(self):
        return "same-name" if self.reuse_name else f"generation-{self.generation}"

    def check(self, directory):
        return Outcome("PASS", "ready")

    def smoke(self, directory):
        if self.interrupt:
            raise KeyboardInterrupt
        if self.unavailable:
            raise RuntimeError("REMOTE_SECRET")
        (directory / "proof.json").write_text('{"observed":true}')
        return Outcome(
            self.smoke_verdict,
            "assertions_checked",
            {"assertion": self.smoke_verdict == "PASS"},
        )

    def delete(self, confirm, directory):
        self.deleted.append(self.generation)
        return Outcome("FAIL" if self.fail_cleanup else "PASS", "absence_checked")


def run(tmp_path, lab, mode="cycle"):
    directory = tmp_path / "run"
    code = validation.validate(lab, mode, directory, "subscription", "group")
    return code, json.loads((directory / "report.json").read_text()), directory


def test_two_generations_are_deleted_and_report_has_local_evidence_links(tmp_path):
    lab = Lab()
    code, report, directory = run(tmp_path, lab)
    assert code == 0 and report["verdict"] == "PASS"
    assert lab.deleted == [1, 2]
    assert report["repository"] == "one-repository"
    assert report["schemaVersion"] == "1.0"
    assert report["cleanupRequested"] is True
    assert report["finishedUtc"]
    for phase in report["phases"]:
        assert phase["durationSeconds"] >= 0 and phase["finishedUtc"]
        assert all((directory / p).is_file() for p in phase["evidence"])
    assert "[proof.json]" in (directory / "report.md").read_text()


@pytest.mark.parametrize("smoke_verdict,code", [("FAIL", 1), ("INCONCLUSIVE", 2)])
def test_smoke_failure_is_not_hidden_by_successful_cleanup(
    tmp_path, smoke_verdict, code
):
    lab = Lab()
    lab.smoke_verdict = smoke_verdict
    result, report, _ = run(tmp_path, lab)
    assert result == code and report["verdict"] == smoke_verdict
    assert lab.deleted == [1, 2]


def test_partial_setup_still_attempts_cleanup_and_does_not_leak_remote_error(tmp_path):
    lab = Lab()
    lab.fail_setup = True
    code, report, directory = run(tmp_path, lab)
    assert code == 2 and lab.deleted == [1]
    assert "REMOTE_SECRET" not in (directory / "report.json").read_text()
    assert report["phases"][-1]["id"] == "generation-1-cleanup"


def test_failed_cleanup_prevents_recreation_and_cannot_pass(tmp_path):
    lab = Lab()
    lab.fail_cleanup = True
    code, report, _ = run(tmp_path, lab)
    assert code == 1 and report["verdict"] == "FAIL"
    assert lab.generation == 1


def test_reused_names_fail_and_second_group_is_cleaned(tmp_path):
    lab = Lab()
    lab.reuse_name = True
    code, report, _ = run(tmp_path, lab)
    assert code == 1 and lab.deleted == [1, 2]
    assert (
        next(p for p in report["phases"] if p["id"] == "recreation")["verdict"]
        == "FAIL"
    )


def test_smoke_mode_leaves_existing_lab_intact(tmp_path):
    lab = Lab()
    code, report, _ = run(tmp_path, lab, "smoke")
    assert code == 0 and lab.generation == 0 and lab.deleted == []
    assert report["cleanupRequested"] is False
    assert [p["id"] for p in report["phases"]] == ["check", "smoke"]


def test_interrupt_keeps_report_and_attempts_cycle_cleanup(tmp_path):
    lab = Lab()
    lab.interrupt = True
    code, report, _ = run(tmp_path, lab)
    assert code == 2 and lab.deleted == [1]
    assert any(p["reason"] == "interrupted" for p in report["phases"])


def test_preflight_refusal_never_creates_or_deletes_a_lab(tmp_path):
    lab = Lab()
    lab.preflight = lambda *args: Outcome("INCONCLUSIVE", "existing_group_refused")
    code, report, _ = run(tmp_path, lab)
    assert code == 2 and lab.generation == 0 and lab.deleted == []
    assert len(report["phases"]) == 1


def test_missing_evidence_or_command_error_is_inconclusive(tmp_path):
    lab = Lab()
    lab.unavailable = True
    code, report, _ = run(tmp_path, lab, "smoke")
    assert code == 2 and report["verdict"] == "INCONCLUSIVE"
