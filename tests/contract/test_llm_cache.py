import json

import pytest

from common import llm


def _key(**kw):
    base = dict(backend="gemma", model="m", prompt="p", seed=7, temperature=0.0, system="", num_ctx=8192)
    base.update(kw)
    return llm.cache_key(**base)


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    demo, own = tmp_path / "demo", tmp_path / "own"
    demo.mkdir()
    monkeypatch.setenv("CT_DEMO_DIR", str(demo))
    monkeypatch.setenv("CT_CACHE_DIR", str(own))
    monkeypatch.setenv("BACKEND", "gemma")
    monkeypatch.setenv("NUM_CTX", "8192")
    monkeypatch.setenv("CACHE_ONLY", "1")
    return demo, own


def test_demo_cache_is_read_and_cache_only_misses(dirs):
    demo, _ = dirs
    (demo / f"{_key()}.json").write_text(json.dumps({"text": "cached!"}))
    assert llm.complete("p", model="m", seed=7, temperature=0.0) == "cached!"
    with pytest.raises(llm.CacheMiss):
        llm.complete("p", model="m", seed=8, temperature=0.0)


def test_key_covers_backend_and_context():
    assert len({_key(), _key(backend="frontier"), _key(num_ctx=4096)}) == 3


def test_live_call_is_cached_atomically(dirs, monkeypatch):
    _, own = dirs
    monkeypatch.delenv("CACHE_ONLY")
    monkeypatch.setattr(llm, "_ollama", lambda *a: "fresh")
    assert llm.complete("p", model="m", seed=1) == "fresh"
    files = list(own.iterdir())
    assert len(files) == 1 and files[0].suffix == ".json"
    monkeypatch.setattr(llm, "_ollama", lambda *a: pytest.fail("should hit cache"))
    assert llm.complete("p", model="m", seed=1) == "fresh"


def test_judge_must_differ(monkeypatch):
    for role, value in (("ATTACKER", "a"), ("BOT", "a"), ("JUDGE", "a")):
        monkeypatch.setenv(f"{role}_MODEL", value)
    with pytest.raises(RuntimeError):
        llm.assert_independent_judge()
    monkeypatch.setenv("JUDGE_MODEL", "j")
    llm.assert_independent_judge()
