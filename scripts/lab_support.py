"""Azure CLI helpers for an isolated, recorded demo lab (no SDK dependency)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class LabError(RuntimeError):
    """An operator-readable failure without credentials or remote response bodies."""


def az(*args: str, subscription: str | None = None):
    executable = shutil.which("az")
    if not executable:
        raise LabError("Azure CLI is required. Run az login first.")
    command = [executable, *args, "--only-show-errors", "-o", "json"]
    if subscription:
        command += ["--subscription", subscription]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired as exc:
        raise LabError(
            "Azure CLI timed out; retain local state and inspect the lab."
        ) from exc
    if result.returncode:
        raise LabError(
            f"Azure CLI failed: {' '.join(args[:3])}. Check login, quota and permissions."
        )
    if "--help" in args:
        return result.stdout
    return json.loads(result.stdout) if result.stdout.strip() else None


def save(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def owned_group(state: dict):
    subscription = state["subscriptionId"]
    if not az(
        "group", "exists", "-n", state["resourceGroup"], subscription=subscription
    ):
        raise LabError(
            "Recorded lab group is absent. Run setup to create a new generation."
        )
    group = az("group", "show", "-n", state["resourceGroup"], subscription=subscription)
    expected = f"/subscriptions/{subscription}/resourceGroups/{state['resourceGroup']}"
    if (
        group["id"].lower() != expected.lower()
        or (group.get("tags") or {}).get("lab-generation") != state["generation"]
    ):
        raise LabError("Group ownership differs from recorded state; mutation refused.")
    return group


def prepare(root: Path, project: str, subscription: str, group: str, config: dict):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", group):
        raise LabError(
            "Use letters, numbers, underscores or hyphens in the group name."
        )
    account = az("account", "show", subscription=subscription)
    subscription = account["id"]
    path = root / ".local" / "lab.json"
    exists = az("group", "exists", "-n", group, subscription=subscription)
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if (
            previous["subscriptionId"] != subscription
            or previous["resourceGroup"] != group
        ):
            raise LabError(
                "Local state targets another subscription/group. Keep one lab per checkout."
            )
        if exists:
            if previous["config"] != config:
                raise LabError("Existing lab uses different setup parameters.")
            owned_group(previous)
            return path, previous
        retired = root / ".local" / "retired" / f"{previous['generation']}.json"
        retired.parent.mkdir(parents=True, exist_ok=True)
        path.replace(retired)
    elif exists:
        raise LabError(
            "Group already exists without matching local state. Choose a new group."
        )
    generation = uuid.uuid4().hex
    state = {
        "subscriptionId": subscription,
        "resourceGroup": group,
        "project": project,
        "generation": generation,
        "config": config,
        "ready": False,
        "accountName": f"{project}-{generation[:10]}",
    }
    save(path, state)  # Save before any cloud mutation, including group creation.
    az(
        "group",
        "create",
        "-n",
        group,
        "-l",
        config["location"],
        "--tags",
        f"project={project}",
        f"lab-generation={generation}",
        subscription=subscription,
    )
    return path, state


def load(root: Path):
    path = root / ".local" / "lab.json"
    if not path.exists():
        raise LabError("Missing .local/lab.json. Run setup first.")
    return path, json.loads(path.read_text(encoding="utf-8"))


def delete(root: Path, confirmation: str | None = None):
    path, state = load(root)
    subscription, group = state["subscriptionId"], state["resourceGroup"]
    if az("group", "exists", "-n", group, subscription=subscription):
        owned_group(state)
        print(f"Delete recorded lab: {group} (subscription {subscription})")
        if confirmation is None:
            confirmation = input(f"Type {group} to delete: ")
        if confirmation != group:
            raise LabError("Confirmation did not match; no resources changed.")
        az("group", "delete", "-n", group, "--yes", subscription=subscription)
    if az("group", "exists", "-n", group, subscription=subscription):
        raise LabError("Lab group remains; cleanup is incomplete.")
    state["ready"] = False
    state["deleted"] = True
    save(path, state)
    print(
        "Verified: recorded lab group is absent. Local evidence and state are retained."
    )
    print(
        "Soft-deleted Cognitive Services accounts may remain recoverable; names are not reused."
    )


def grant(state: dict, principal: str, role: str, scope: str, kind="ServicePrincipal"):
    for attempt in range(12):
        try:
            return az(
                "role",
                "assignment",
                "create",
                "--assignee-object-id",
                principal,
                "--assignee-principal-type",
                kind,
                "--role",
                role,
                "--scope",
                scope,
                subscription=state["subscriptionId"],
            )
        except LabError:
            if attempt == 11:
                raise
            time.sleep(10)


def request(
    state: dict,
    url: str,
    method="GET",
    payload=None,
    audience="https://search.azure.com/",
):
    token = az(
        "account",
        "get-access-token",
        "--resource",
        audience,
        subscription=state["subscriptionId"],
    )["accessToken"]
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    for attempt in range(18):
        try:
            with urlopen(
                Request(url, body, headers, method=method), timeout=60
            ) as response:
                data = response.read()
                return json.loads(data) if data else None
        except HTTPError as exc:
            # Retry idempotent writes while RBAC or the new service propagates.
            if (
                exc.code in {403, 429, 503}
                and method in {"GET", "PUT"}
                and attempt < 17
            ):
                time.sleep(10)
                continue
            raise LabError(
                f"Azure HTTP {exc.code} during {method}; check RBAC/API/region. State retained."
            ) from None
        except URLError:
            raise LabError("Azure network request failed; state retained.") from None


def child(root: Path, state: dict, args: list[str]):
    import sys

    owned_group(state)
    if not state.get("ready"):
        raise LabError("Setup is incomplete. Rerun setup with the same parameters.")
    env = os.environ.copy()
    env.update(state["environment"])
    # Dedicated labs use Entra auth; never accidentally select an inherited API key/version.
    env["AZURE_OPENAI_API_KEY"] = ""
    az("account", "set", "--subscription", state["subscriptionId"])
    return subprocess.call([sys.executable, *args], cwd=root, env=env)
