"""Configuration and token authentication checks without Azure requests."""

import sys

import pytest


def test_cli_loads_only_current_directory_dotenv_and_preserves_shell(tmp_path, monkeypatch):
    from ci_repair import cli

    (tmp_path / ".env").write_text("LAB_CONFIG_PROBE=file\nLAB_SHELL_PROBE=file\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LAB_CONFIG_PROBE", raising=False)
    monkeypatch.setenv("LAB_SHELL_PROBE", "shell")
    monkeypatch.setattr(sys, "argv", ["ci-repair", "status", "--thread", "unknown"])
    observed = []

    def model(mode):
        import os

        observed.append((os.getenv("LAB_CONFIG_PROBE"), os.getenv("LAB_SHELL_PROBE")))
        raise RuntimeError("Stop before graph execution")

    monkeypatch.setattr(cli, "model_for", model)
    with pytest.raises(RuntimeError, match="Stop before"):
        cli.main()
    assert observed == [("file", "shell")]


def test_azure_mode_supports_cli_token_provider_without_api_key(monkeypatch):
    import azure.identity
    import langchain_openai

    from ci_repair.graph import Proposal, model_for

    monkeypatch.setenv("AZURE_OPENAI_USE_ENTRA", "1")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "recorded")
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    credential, provider, captured = object(), object(), {}
    monkeypatch.setattr(azure.identity, "AzureCliCredential", lambda: credential)

    def token_provider(identity, scope):
        assert identity is credential
        assert scope == "https://cognitiveservices.azure.com/.default"
        return provider

    class Model:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def with_structured_output(self, schema):
            assert schema is Proposal
            return self

    monkeypatch.setattr(azure.identity, "get_bearer_token_provider", token_provider)
    monkeypatch.setattr(langchain_openai, "AzureChatOpenAI", Model)
    model_for("azure")
    assert captured["azure_ad_token_provider"] is provider
    assert captured["azure_deployment"] == "recorded"
