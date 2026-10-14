"""Create/run/delete a dedicated Azure OpenAI lab for the local LangGraph PoC."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from uuid import uuid4

from lab_support import (
    LabError,
    az,
    child,
    delete,
    grant,
    load,
    owned_group,
    prepare,
    save,
)

ROOT = Path(__file__).resolve().parents[1]


def setup(args):
    config = {
        "location": args.location,
        "model": args.model,
        "version": args.model_version,
        "capacity": args.capacity,
    }
    path, state = prepare(ROOT, "ci-repair", args.subscription, args.group, config)
    subscription, group, name = (
        state["subscriptionId"],
        state["resourceGroup"],
        state["accountName"],
    )
    state["ready"] = False
    save(path, state)
    az(
        "provider",
        "register",
        "--namespace",
        "Microsoft.CognitiveServices",
        "--wait",
        subscription=subscription,
    )
    account = az(
        "cognitiveservices",
        "account",
        "create",
        "-n",
        name,
        "-g",
        group,
        "-l",
        args.location,
        "--kind",
        "OpenAI",
        "--sku",
        "S0",
        "--custom-domain",
        name,
        "--yes",
        subscription=subscription,
    )
    az(
        "cognitiveservices",
        "account",
        "deployment",
        "create",
        "-n",
        name,
        "-g",
        group,
        "--deployment-name",
        "ci-repair-model",
        "--model-name",
        args.model,
        "--model-version",
        args.model_version,
        "--model-format",
        "OpenAI",
        "--sku-name",
        "GlobalStandard",
        "--sku-capacity",
        str(args.capacity),
        subscription=subscription,
    )
    token = az("account", "get-access-token", subscription=subscription)["accessToken"]
    claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    if not claims.get("oid"):
        raise LabError(
            "Signed-in token has no object ID. Use a CLI user or service principal login."
        )
    grant(
        state,
        claims["oid"],
        "Cognitive Services OpenAI User",
        account["id"],
        "ServicePrincipal" if claims.get("idtyp") == "app" else "User",
    )
    state["environment"] = {
        "AZURE_OPENAI_ENDPOINT": account["properties"]["endpoint"],
        "AZURE_OPENAI_DEPLOYMENT": "ci-repair-model",
        "OPENAI_API_VERSION": "2024-10-21",
        "AZURE_OPENAI_USE_ENTRA": "1",
    }
    state["ready"] = True
    save(path, state)
    print(
        "SETUP COMPLETE. Run python scripts/lab.py run --scenario S01 to verify live inference."
    )
    print(
        "RBAC can take several minutes to propagate. No model request was made during setup."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    provision = commands.add_parser(
        "setup", help="Create resources in a dedicated group"
    )
    provision.add_argument("--subscription", required=True)
    provision.add_argument("--group", default="rg-langgraph-ci-repair-demo")
    provision.add_argument("--location", default="swedencentral")
    provision.add_argument("--model", default="gpt-4.1-mini")
    provision.add_argument("--model-version", default="2025-04-14")
    provision.add_argument("--capacity", type=int, choices=range(1, 101), default=10)
    run = commands.add_parser(
        "run", help="Run a live Azure scenario, stopping at human approval"
    )
    run.add_argument("--scenario", choices=["S01", "S02", "S03", "S04"], default="S01")
    run.add_argument(
        "--thread", help="A new thread ID; a UUID is generated when omitted"
    )
    commands.add_parser("check", help="Read-only group ownership/setup check")
    cleanup = commands.add_parser(
        "delete", help="Delete and verify the dedicated group"
    )
    cleanup.add_argument("--confirm", help="Exact recorded group name (noninteractive)")
    args = parser.parse_args()
    if args.action == "setup":
        setup(args)
        return 0
    if args.action == "delete":
        delete(ROOT, args.confirm)
        return 0
    _, state = load(ROOT)
    if args.action == "check":
        owned_group(state)
        print(
            f"Recorded group ownership verified; setup complete: {state.get('ready', False)}"
        )
        return 0
    return child(
        ROOT,
        state,
        [
            "-m",
            "ci_repair.cli",
            "run",
            "--mode",
            "azure",
            "--scenario",
            args.scenario,
            "--thread",
            args.thread or uuid4().hex,
        ],
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (LabError, OSError, KeyError, ValueError) as exc:
        print(
            str(exc)
            if isinstance(exc, LabError)
            else "Lab operation failed; check local state and configuration.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print(
            "Interrupted. Resources retained; delete after saving evidence.",
            file=sys.stderr,
        )
        raise SystemExit(130) from None
