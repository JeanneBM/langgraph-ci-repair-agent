"""One repository, one report: live smoke checks or a two-generation lab cycle."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

VERDICTS = {"PASS", "FAIL", "INCONCLUSIVE"}


@dataclass
class Outcome:
    verdict: str
    reason: str
    checks: dict = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)


class Unavailable(RuntimeError):
    """A prerequisite, command or evidence collection failed."""


def utc():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def execute(command, root, directory, *, env=None, input_text=None, timeout=1800):
    """Keep raw output local; return codes alone never establish a smoke pass."""
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / f"command-{uuid4().hex[:8]}.log"
    with log.open("w", encoding="utf-8") as stream:
        try:
            result = subprocess.run(
                command,
                cwd=root,
                env=env,
                input=input_text,
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Unavailable("command_unavailable_or_timed_out") from exc
    if result.returncode:
        raise Unavailable("command_failed")
    return log


def azure(*arguments, subscription):
    executable = shutil.which("az")
    if not executable:
        raise Unavailable("azure_cli_missing")
    result = subprocess.run(
        [
            executable,
            *arguments,
            "--subscription",
            subscription,
            "--only-show-errors",
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode:
        raise Unavailable("azure_operation_failed")
    return json.loads(result.stdout) if result.stdout.strip() else None


def selected_environment(state):
    env = os.environ.copy()
    env.update(state["environment"])
    env["AZURE_OPENAI_API_KEY"] = ""
    azure("account", "set", subscription=state["subscriptionId"])
    return env


class Report:
    def __init__(self, directory, repository, mode):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=False)
        self.data = {
            "schemaVersion": "1.0",
            "runId": directory.name,
            "repository": repository,
            "mode": mode,
            "execution": "live",
            "startedUtc": utc(),
            "finishedUtc": None,
            "verdict": "INCONCLUSIVE",
            "phases": [],
            "cleanupRequested": mode == "cycle",
        }
        self.write()

    def write(self):
        path = self.directory / "report.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
        rows = [
            "# Lab validation",
            "",
            f"Repository: `{self.data['repository']}`",
            f"Run: `{self.data['runId']}`",
            f"Mode: `{self.data['mode']}`",
            f"Result: **{self.data['verdict']}**",
            "",
            "| Phase | Result | Seconds | Reason | Evidence |",
            "| --- | --- | ---: | --- | --- |",
        ]
        for phase in self.data["phases"]:
            links = ", ".join(f"[{Path(p).name}]({p})" for p in phase["evidence"])
            rows.append(
                f"| {phase['id']} | {phase['verdict']} | {phase['durationSeconds']} | "
                f"{phase['reason']} | {links} |"
            )
        rows += [
            "",
            "Raw logs are local evidence. Review them before sharing.",
            "Smoke mode leaves the existing Azure lab provisioned.",
        ]
        (self.directory / "report.md").write_text(
            "\n".join(rows) + "\n", encoding="utf-8"
        )

    def phase(self, name, operation):
        directory = self.directory / name
        directory.mkdir()
        start, clock = utc(), time.monotonic()
        phase = {
            "id": name,
            "startedUtc": start,
            "finishedUtc": None,
            "durationSeconds": 0,
            "verdict": "INCONCLUSIVE",
            "reason": "running",
            "checks": {},
            "evidence": [],
        }
        self.data["phases"].append(phase)
        self.write()  # Persist an unfinished phase before invoking cloud commands.
        interrupted = False
        try:
            result = operation(directory)
            if not isinstance(result, Outcome) or result.verdict not in VERDICTS:
                raise Unavailable("invalid_adapter_result")
        except KeyboardInterrupt:
            interrupted = True
            result = Outcome("INCONCLUSIVE", "interrupted")
        except Exception:
            # Remote exception bodies can contain credentials and documents.
            result = Outcome("INCONCLUSIVE", "execution_or_evidence_error")
        phase.update(
            asdict(result),
            finishedUtc=utc(),
            durationSeconds=round(time.monotonic() - clock, 3),
        )
        # Link only evidence inside this run, including partial-command logs.
        phase["evidence"] = [
            p.relative_to(self.directory).as_posix()
            for p in sorted(directory.rglob("*"))
            if p.is_file()
        ]
        self.write()
        if interrupted:
            raise KeyboardInterrupt
        return result

    def finish(self):
        statuses = [p["verdict"] for p in self.data["phases"]]
        self.data["verdict"] = (
            "FAIL"
            if "FAIL" in statuses
            else "INCONCLUSIVE"
            if not statuses or "INCONCLUSIVE" in statuses
            else "PASS"
        )
        self.data["finishedUtc"] = utc()
        self.write()
        return {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2}[self.data["verdict"]]


def validate(adapter, mode, directory, subscription=None, confirm_delete=None):
    report = Report(directory, adapter.repository, mode)
    try:
        if mode == "smoke":
            checked = report.phase("check", adapter.check)
            if checked.verdict == "PASS":
                report.phase("smoke", adapter.smoke)
        else:
            preflight = report.phase(
                "preflight",
                lambda d: adapter.preflight(subscription, confirm_delete, d),
            )
            if preflight.verdict != "PASS":
                return report.finish()
            first_identity = None
            for generation in (1, 2):
                prefix = f"generation-{generation}"
                created = None
                try:
                    created = report.phase(
                        prefix + "-setup", lambda d: adapter.setup(subscription, d)
                    )
                    if created.verdict != "PASS":
                        break
                    identity = adapter.identity()
                    if generation == 1:
                        first_identity = identity
                    else:
                        changed = bool(
                            identity and first_identity and identity != first_identity
                        )
                        result = report.phase(
                            "recreation",
                            lambda d, changed=changed: Outcome(
                                "PASS" if changed else "FAIL",
                                "fresh_generation"
                                if changed
                                else "resource_names_reused",
                            ),
                        )
                        if result.verdict != "PASS":
                            break
                    checked = report.phase(prefix + "-check", adapter.check)
                    if checked.verdict == "PASS":
                        report.phase(prefix + "-smoke", adapter.smoke)
                finally:
                    cleaned = report.phase(
                        prefix + "-cleanup", lambda d: adapter.delete(confirm_delete, d)
                    )
                if (
                    created is None
                    or created.verdict != "PASS"
                    or cleaned.verdict != "PASS"
                ):
                    break
    except KeyboardInterrupt:
        # Cycle finally cleanup is attempted once, and the report survives interruption.
        pass
    except Exception:
        report.phase(
            "execution",
            lambda d: Outcome("INCONCLUSIVE", "execution_or_evidence_error"),
        )
    return report.finish()


def main(adapter):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["smoke", "cycle"])
    parser.add_argument("--subscription", help="Required for cycle mode")
    parser.add_argument(
        "--confirm-delete", help="Exact dedicated group name; required for cycle"
    )
    args = parser.parse_args()
    if args.mode == "cycle" and (
        not args.subscription or args.confirm_delete != adapter.group
    ):
        parser.error(
            f"cycle requires --subscription and --confirm-delete {adapter.group}"
        )
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex[:6]
    directory = adapter.root / ".local/reports" / run_id
    code = validate(
        adapter, args.mode, directory, args.subscription, args.confirm_delete
    )
    print(f"Report: {directory / 'report.md'} (exit={code})")
    return code
