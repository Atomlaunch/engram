"""NUBIS-001: reasoning-only completions must keep their full multi-line JSON."""
import json
import sys
import types

from engram import ingest

EXPECTED = {"entities": [{"name": "Shana", "type": "person"}], "facts": [{"fact": "Shana likes Dr Pepper at night"}]}


def _fake_requests(message):
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": message}]}

    mod = types.ModuleType("requests")
    mod.post = lambda *a, **k: Resp()
    return mod


def _call(monkeypatch, message):
    monkeypatch.setitem(sys.modules, "requests", _fake_requests(message))
    return ingest._call_llm("chunk", "m", "k", base_url="http://local/v1")


def test_reasoning_only_multiline_json_is_parsed(monkeypatch):
    reasoning = "Let me extract the facts.\n\n" + json.dumps(EXPECTED, indent=2) + "\n\nThat covers it."
    assert _call(monkeypatch, {"content": "", "reasoning_content": reasoning}) == EXPECTED


def test_reasoning_only_fenced_json_is_parsed(monkeypatch):
    reasoning = "Thinking...\n```json\n" + json.dumps(EXPECTED, indent=2) + "\n```\n"
    assert _call(monkeypatch, {"content": None, "reasoning_content": reasoning}) == EXPECTED


def test_reasoning_with_braces_in_strings(monkeypatch):
    data = {"entities": [], "facts": [{"fact": "uses {curly} braces and a \"quote\""}]}
    reasoning = "pre {not json} " + json.dumps(data, indent=2)
    assert _call(monkeypatch, {"content": "", "reasoning_content": reasoning}) == data


def test_non_json_reasoning_fails_safely(monkeypatch):
    assert _call(monkeypatch, {"content": "", "reasoning_content": "I could not find anything worth storing."}) is None


def test_plain_content_still_works(monkeypatch):
    assert _call(monkeypatch, {"content": "```json\n" + json.dumps(EXPECTED) + "\n```"}) == EXPECTED
