"""Repository-local adapters for Foundry RAG and LangGraph demo labs."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from lab_validation import Outcome, Unavailable, azure, execute, selected_environment


class CloudLab:
    def __init__(self, root, repository, group, kind):
        self.root, self.repository, self.group, self.kind = (
            root,
            repository,
            group,
            kind,
        )
        self.subscription = None

    def state(self):
        state = json.loads((self.root / ".local/lab.json").read_text(encoding="utf-8"))
        if state["resourceGroup"] != self.group:
            raise Unavailable("unexpected_recorded_group")
        return state

    def preflight(self, subscription, confirmation, directory):
        if confirmation != self.group:
            return Outcome("INCONCLUSIVE", "confirmation_mismatch")
        self.subscription = azure("account", "show", subscription=subscription)["id"]
        if azure("group", "exists", "-n", self.group, subscription=self.subscription):
            return Outcome("INCONCLUSIVE", "cycle_requires_absent_group")
        path = self.root / ".local/lab.json"
        if path.exists() and self.state()["subscriptionId"] != self.subscription:
            return Outcome("INCONCLUSIVE", "recorded_subscription_mismatch")
        return Outcome("PASS", "isolated_cycle_preflight")

    def setup(self, subscription, directory):
        execute(
            [
                sys.executable,
                "scripts/lab.py",
                "setup",
                "--subscription",
                subscription,
                "--group",
                self.group,
            ],
            self.root,
            directory,
        )
        return Outcome("PASS", "setup_completed")

    def identity(self):
        return self.state()["accountName"]

    def check(self, directory):
        from lab_support import owned_group

        state = self.state()
        owned_group(state)
        if not state.get("ready"):
            return Outcome("INCONCLUSIVE", "setup_incomplete")
        name = "rag-model" if self.kind == "rag" else "ci-repair-model"
        model = azure(
            "cognitiveservices",
            "account",
            "deployment",
            "show",
            "-g",
            self.group,
            "-n",
            state["accountName"],
            "--deployment-name",
            name,
            subscription=state["subscriptionId"],
        )
        checks = {
            "groupOwnership": True,
            "modelReady": model["properties"]["provisioningState"] == "Succeeded",
            "modelMatches": model["properties"]["model"]["name"]
            == state["config"]["model"],
            "modelVersionMatches": model["properties"]["model"]["version"]
            == state["config"]["version"],
        }
        if self.kind == "rag":
            checks["manifestPresent"] = Path(
                state["environment"]["DEPLOYMENT_FILE"]
            ).is_file()
        (directory / "checks.json").write_text(
            json.dumps(checks, indent=2), encoding="utf-8"
        )
        return Outcome(
            "PASS" if all(checks.values()) else "INCONCLUSIVE",
            "readiness_checked",
            checks,
        )

    def delete(self, confirmation, directory):
        path = self.root / ".local/lab.json"
        if path.exists():
            state = self.state()
            subscription = state["subscriptionId"]
            execute(
                [sys.executable, "scripts/lab.py", "delete", "--confirm", confirmation],
                self.root,
                directory,
            )
        else:
            subscription = self.subscription
        absent = not azure(
            "group", "exists", "-n", self.group, subscription=subscription
        )
        (directory / "cleanup.json").write_text(
            json.dumps({"groupAbsent": absent}), encoding="utf-8"
        )
        return Outcome(
            "PASS" if absent else "FAIL", "cleanup_verified", {"groupAbsent": absent}
        )

    def smoke(self, directory):
        state = self.state()
        env = selected_environment(state)
        if self.kind == "rag":
            return self.rag_smoke(directory, env)
        return self.graph_smoke(directory, env)

    def rag_smoke(self, directory, env):
        # The guarded client is tested directly; raw answer/document bodies stay out of reports.
        sys.path.insert(0, str(self.root))
        from rag_foundry.clients import project_client, responses_client
        from rag_foundry.config import Settings
        from rag_foundry.deployment import resolve_version
        from rag_foundry.grounding import GroundingError
        from rag_foundry.session import RagSession

        previous = os.environ.copy()
        try:
            os.environ.update(env)
            settings = Settings.from_env()
            version = resolve_version(settings)
            with project_client(settings) as project:
                project.agents.get_version(settings.agent_name, version)
                with responses_client(project, settings) as client:
                    known = RagSession(client, settings, version).ask(
                        "What is the product return period?"
                    )
                    unknown = RagSession(client, settings, version).ask(
                        "What is the private phone number of a person "
                        "not mentioned in the documents?"
                    )
            checks = {
                "knownAnswer": not known.unknown and "30 days" in known.text.casefold(),
                "knownCitations": bool(known.sources),
                "unknownAbstention": unknown.unknown,
            }
            (directory / "smoke.json").write_text(
                json.dumps({"version": version, "checks": checks}, indent=2),
                encoding="utf-8",
            )
            return Outcome(
                "PASS" if all(checks.values()) else "FAIL",
                "known_unknown_answers_checked",
                checks,
            )
        except GroundingError as exc:
            environmental = {
                "retrieval_failed",
                "response_not_completed",
                "message_not_completed",
                "unsupported_retrieval_format",
                "missing_retrieval_output",
            }
            return Outcome(
                "INCONCLUSIVE" if str(exc) in environmental else "FAIL",
                "grounding_check_rejected",
            )
        finally:
            os.environ.clear()
            os.environ.update(previous)

    def graph_smoke(self, directory, env):
        output = directory / "artifacts"
        checks = {}
        for scenario, action in (("S01", "approve"), ("S03", "reject")):
            thread = "smoke-" + uuid4().hex
            cli = [sys.executable, "-m", "ci_repair.cli"]
            execute(
                [
                    *cli,
                    "run",
                    "--mode",
                    "azure",
                    "--scenario",
                    scenario,
                    "--thread",
                    thread,
                    "--output",
                    str(output),
                ],
                self.root,
                directory,
                env=env,
                timeout=240,
            )
            folder = output / thread
            evidence = folder / "evidence.json"
            state = json.loads(evidence.read_text(encoding="utf-8"))
            if state["status"] != "awaiting_approval":
                transport = (
                    state.get("feedback")
                    == "Model response failed or did not match the schema."
                )
                return Outcome(
                    "INCONCLUSIVE" if transport else "FAIL",
                    "approval_gate_not_reached",
                    checks,
                )
            (folder / "approval-gate.json").write_text(
                evidence.read_text(encoding="utf-8"), encoding="utf-8"
            )
            checks[scenario + "ApprovalGate"] = not (folder / "repair.patch").exists()
            execute(
                [*cli, action, "--thread", thread, "--output", str(output)],
                self.root,
                directory,
                env=env,
                timeout=60,
            )
            resumed = json.loads(evidence.read_text(encoding="utf-8"))
            if action == "approve":
                checks[scenario + "ApprovedExport"] = (
                    resumed["status"] == "draft_prepared"
                    and (folder / "repair.patch").is_file()
                    and (folder / "draft-pr.md").is_file()
                    and bool(resumed.get("diff"))
                    and (folder / "repair.patch").read_text(encoding="utf-8")
                    == resumed["diff"]
                    and (folder / "draft-pr.md").read_text(encoding="utf-8")
                    == resumed["draft"]
                )
            else:
                checks[scenario + "RejectedWithoutExport"] = (
                    resumed["status"] == "rejected"
                    and not (folder / "repair.patch").exists()
                )
        return Outcome(
            "PASS" if all(checks.values()) else "FAIL",
            "live_graph_and_restart_approval_checked",
            checks,
        )
