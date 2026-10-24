"""NUBIS-003: `engram ingest` sends completions to the configured endpoint, never a pool one."""
import json
import sys
import types

from click.testing import CliRunner

from engram.cli import cli

EXTRACT = {"entities": [], "facts": [{"fact": "Engram reads the configured endpoint", "confidence": 0.9}]}


def _setup(tmp_path, monkeypatch, with_pool=True):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    (vault / "Daily").mkdir(parents=True)
    (vault / "Daily" / "2026-10-23.md").write_text("# Today\nWe pointed Engram at a local endpoint.\n" * 5)
    cfg = {"vault_path": str(vault), "index_db": str(home / ".engram" / "vault_index.db"),
           "log_level": "warning", "log_path": str(home / ".engram" / "engram.log"),
           "llm_provider": "openai", "llm_base_url": "http://engram-endpoint.local/v1",
           "llm_model": "local-model", "ingest_sources": ["Daily"]}
    (home / ".engram").mkdir(parents=True)
    (home / ".engram" / "config.json").write_text(json.dumps(cfg))
    if with_pool:  # an unrelated Hermes credential pool must NOT win over the configured endpoint
        (home / ".hermes").mkdir()
        (home / ".hermes" / "auth.json").write_text(json.dumps({"credential_pool": {"other": [
            {"access_token": "pool-token-1234567890", "base_url": "http://hermes-pool.local/v1"}]}}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-engram")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    calls = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(EXTRACT)}}]}

    mod = types.ModuleType("requests")
    mod.post = lambda url, **kw: calls.append((url, kw)) or Resp()
    monkeypatch.setitem(sys.modules, "requests", mod)
    return home, calls


def test_cli_ingest_uses_configured_endpoint(tmp_path, monkeypatch):
    home, calls = _setup(tmp_path, monkeypatch)
    res = CliRunner().invoke(cli, ["--config", str(home / ".engram" / "config.json"), "ingest"])
    assert res.exit_code == 0, res.output
    assert calls, "no completion request was sent"
    assert {u for u, _ in calls} == {"http://engram-endpoint.local/v1/chat/completions"}
    assert all(kw["headers"]["Authorization"] == "Bearer sk-test-engram" for _, kw in calls)
    assert all(kw["json"]["model"] == "local-model" for _, kw in calls)


def test_run_ingest_reads_endpoint_from_config(tmp_path, monkeypatch):
    home, calls = _setup(tmp_path, monkeypatch, with_pool=False)
    from engram.ingest import run_ingest
    cfg = json.loads((home / ".engram" / "config.json").read_text())
    run_ingest(vault_root=cfg["vault_path"], source_dirs=["Daily"])
    assert calls and {u for u, _ in calls} == {"http://engram-endpoint.local/v1/chat/completions"}
