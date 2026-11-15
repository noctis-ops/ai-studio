"""T-ADP-* (PHASE2_IMPLEMENTATION_PROMPT §G) — offline adapter tests.

No network: SDKs are either replaced by a fake module or the SDK client is
injected. STUDIO_LIVE_LLM stays unset; zero real spend.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest
from conftest import make_home

from studio.config.loader import load_all
from studio.config.registry import RegistryError
from studio.core.gateway import CallContext
from studio.core.pricing import actual_cost_musd
from studio.db import Database, migrate
from studio.domain.errors import (
    ConfigError,
    PermanentProviderError,
    TransientProviderError,
)
from studio.providers.anthropic_llm import AnthropicLLMProvider
from studio.providers.base import LLMRequest
from studio.providers.factory import build_provider
from studio.providers.openai_llm import OpenAILLMProvider

REPO_ROOT = Path(__file__).resolve().parents[1]


def _config(tmp_path, monkeypatch=None, filename="adp"):
    home = make_home(tmp_path, filename=filename)
    if monkeypatch is not None:
        monkeypatch.setenv("STUDIO_HOME", str(home))
    return load_all(home)


def _anthropic_entry(config):
    return config.registry.models["llm.creative"]


def _openai_entry(config):
    return config.registry.models["llm.light"]


class _FakeAnthropicMessages:
    def __init__(self, *, response=None, error: Exception | None = None):
        self._response = response
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


class _FakeAnthropicClient:
    def __init__(self, *, response=None, error: Exception | None = None):
        self.messages = _FakeAnthropicMessages(response=response, error=error)


class _FakeOpenAICompletions:
    def __init__(self, *, response=None, error: Exception | None = None):
        self._response = response
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


class _FakeOpenAIChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeOpenAIClient:
    def __init__(self, *, response=None, error: Exception | None = None):
        self.chat = _FakeOpenAIChat(_FakeOpenAICompletions(response=response, error=error))


def _anthropic_response(*, text='{"ok": true}', model="claude-sonnet-4-5-20250929",
                        resp_id="msg_0123", in_tok=1200, out_tok=350):
    block = types.SimpleNamespace(type="text", text=text)
    usage = types.SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok)
    return types.SimpleNamespace(content=[block], usage=usage, model=model, id=resp_id)


def _openai_response(*, text='{"ok": true}', model="gpt-4o-mini",
                     resp_id="chatcmpl-123", in_tok=1200, out_tok=350):
    message = types.SimpleNamespace(content=text)
    choice = types.SimpleNamespace(message=message)
    usage = types.SimpleNamespace(prompt_tokens=in_tok, completion_tokens=out_tok)
    return types.SimpleNamespace(choices=[choice], usage=usage, model=model, id=resp_id)


class _StatusError(Exception):
    def __init__(self, status_code: int, message: str = "boom"):
        super().__init__(message)
        self.status_code = status_code


def _api_timeout(name: str) -> Exception:
    cls = type(name, (Exception,), {})
    return cls("timed out")


def _req(**kwargs) -> LLMRequest:
    base = {"system": "system-pack", "prompt": "prompt-body", "max_tokens": 64,
            "temperature": 0.7}
    base.update(kwargs)
    return LLMRequest(**base)


# --------------------------------------------------------------- T-ADP-01


@pytest.mark.asyncio
async def test_anthropic_request_built_correctly(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    entry = _anthropic_entry(config)
    captured = {}

    class _CapturingMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _anthropic_response()

    class _FakeSDK:
        class AsyncAnthropic:
            def __init__(self, *, api_key, timeout, max_retries):
                captured["_timeout"] = timeout
                captured["_max_retries"] = max_retries
                captured["_api_key"] = api_key
                self.messages = _CapturingMessages()

    monkeypatch.setitem(sys.modules, "anthropic", _FakeSDK)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-marker-12345")
    adapter = AnthropicLLMProvider(entry)
    result = await adapter.complete(_req(expect_json=True))
    assert captured["model"] == "claude-sonnet-4-5-20250929"
    assert captured["max_tokens"] == 64
    assert captured["temperature"] == 0.7
    assert captured["system"] == "system-pack"
    assert captured["messages"] == [{"role": "user", "content": "prompt-body"}]
    assert captured["_timeout"] == 60.0
    assert captured["_max_retries"] == 0
    assert "stream" not in captured and "tools" not in captured
    assert result.parsed_json == {"ok": True}


# --------------------------------------------------------------- T-ADP-02


@pytest.mark.asyncio
async def test_anthropic_usage_maps_to_hand_computed_musd(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    entry = _anthropic_entry(config)
    client = _FakeAnthropicClient(response=_anthropic_response(in_tok=1200, out_tok=350))
    adapter = AnthropicLLMProvider(entry, client=client)
    result = await adapter.complete(_req())
    assert result.tokens_in == 1200 and result.tokens_out == 350
    # in=1200 @ $3/1M = 3600 musd; out=350 @ $15/1M = 5250 musd → 8850
    assert actual_cost_musd(entry, _req(), result) == 8850


# --------------------------------------------------------------- T-ADP-03


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [(400, PermanentProviderError), (401, PermanentProviderError),
     (403, PermanentProviderError), (404, PermanentProviderError),
     (413, PermanentProviderError),
     (429, TransientProviderError), (500, TransientProviderError),
     (502, TransientProviderError), (529, TransientProviderError)],
)
async def test_anthropic_error_mapping(tmp_path, monkeypatch, status, expected):
    config = _config(tmp_path, monkeypatch)
    entry = _anthropic_entry(config)
    client = _FakeAnthropicClient(error=_StatusError(status))
    adapter = AnthropicLLMProvider(entry, client=client)
    with pytest.raises(expected):
        await adapter.complete(_req())


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["APITimeoutError", "APIConnectionError"])
async def test_anthropic_timeout_and_connection_are_transient(tmp_path, monkeypatch, name):
    config = _config(tmp_path, monkeypatch)
    entry = _anthropic_entry(config)
    client = _FakeAnthropicClient(error=_api_timeout(name))
    adapter = AnthropicLLMProvider(entry, client=client)
    with pytest.raises(TransientProviderError):
        await adapter.complete(_req())


# --------------------------------------------------------------- T-ADP-04


@pytest.mark.asyncio
async def test_anthropic_empty_content_is_transient(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    entry = _anthropic_entry(config)
    client = _FakeAnthropicClient(response=_anthropic_response(text="   "))
    adapter = AnthropicLLMProvider(entry, client=client)
    with pytest.raises(TransientProviderError):
        await adapter.complete(_req())


# --------------------------------------------------------------- T-ADP-05


@pytest.mark.asyncio
async def test_openai_request_built_correctly(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    entry = _openai_entry(config)
    client = _FakeOpenAIClient(response=_openai_response())
    adapter = OpenAILLMProvider(entry, client=client)
    await adapter.complete(_req(expect_json=True))
    (kwargs,) = client.chat.completions.calls
    assert kwargs["model"] == "gpt-4o-mini"  # alias used verbatim
    assert kwargs["max_tokens"] == 64 and kwargs["temperature"] == 0.7
    assert kwargs["messages"] == [
        {"role": "system", "content": "system-pack"},
        {"role": "user", "content": "prompt-body"},
    ]
    assert kwargs["response_format"] == {"type": "json_object"}
    assert "stream" not in kwargs and "tools" not in kwargs


@pytest.mark.asyncio
async def test_openai_expect_json_absent_sets_no_response_format(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    entry = _openai_entry(config)
    client = _FakeOpenAIClient(response=_openai_response(text="plain text"))
    adapter = OpenAILLMProvider(entry, client=client)
    result = await adapter.complete(_req(expect_json=False))
    (kwargs,) = client.chat.completions.calls
    assert "response_format" not in kwargs
    assert result.parsed_json is None


# --------------------------------------------------------------- T-ADP-06


@pytest.mark.asyncio
async def test_openai_usage_maps_to_hand_computed_musd(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    entry = _openai_entry(config)
    client = _FakeOpenAIClient(response=_openai_response(in_tok=1200, out_tok=350))
    adapter = OpenAILLMProvider(entry, client=client)
    result = await adapter.complete(_req())
    # in=1200 @ $0.15/1M = 180 musd; out=350 @ $0.60/1M = 210 musd → 390
    assert actual_cost_musd(entry, _req(), result) == 390
    assert result.raw_ref == "gpt-4o-mini:chatcmpl-123"


# --------------------------------------------------------------- T-ADP-07


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [(400, PermanentProviderError), (401, PermanentProviderError),
     (403, PermanentProviderError), (404, PermanentProviderError),
     (413, PermanentProviderError),
     (429, TransientProviderError), (500, TransientProviderError),
     (502, TransientProviderError), (529, TransientProviderError)],
)
async def test_openai_error_mapping(tmp_path, monkeypatch, status, expected):
    config = _config(tmp_path, monkeypatch)
    entry = _openai_entry(config)
    client = _FakeOpenAIClient(error=_StatusError(status))
    adapter = OpenAILLMProvider(entry, client=client)
    with pytest.raises(expected):
        await adapter.complete(_req())


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["APITimeoutError", "APIConnectionError"])
async def test_openai_timeout_and_connection_are_transient(tmp_path, monkeypatch, name):
    config = _config(tmp_path, monkeypatch)
    entry = _openai_entry(config)
    client = _FakeOpenAIClient(error=_api_timeout(name))
    adapter = OpenAILLMProvider(entry, client=client)
    with pytest.raises(TransientProviderError):
        await adapter.complete(_req())


@pytest.mark.asyncio
async def test_openai_empty_content_is_transient(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    entry = _openai_entry(config)
    client = _FakeOpenAIClient(response=_openai_response(text=""))
    adapter = OpenAILLMProvider(entry, client=client)
    with pytest.raises(TransientProviderError):
        await adapter.complete(_req())


@pytest.mark.asyncio
async def test_invalid_json_from_model_is_permanent(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    for entry_name, client in (("llm.creative", _FakeAnthropicClient),
                               ("llm.light", _FakeOpenAIClient)):
        entry = config.registry.models[entry_name]
        response = (
            _anthropic_response(text="{not json")
            if entry_name == "llm.creative"
            else _openai_response(text="{not json")
        )
        client = client(response=response)
        adapter = (
            AnthropicLLMProvider(entry, client=client)
            if entry_name == "llm.creative"
            else OpenAILLMProvider(entry, client=client)
        )
        with pytest.raises(PermanentProviderError) as excinfo:
            await adapter.complete(_req(expect_json=True))
        assert "invalid JSON" in str(excinfo.value)


# --------------------------------------------------------------- T-ADP-08


def test_missing_anthropic_sdk_raises_config_error(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)
    config = _config(tmp_path, monkeypatch)
    entry = _anthropic_entry(config)
    adapter = AnthropicLLMProvider(entry, api_key="sk-ant-marker-12345")
    with pytest.raises(ConfigError, match="ai-studio\\[providers\\]"):
        adapter._get_client()


def test_missing_openai_sdk_raises_config_error(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", None)
    config = _config(tmp_path, monkeypatch)
    entry = _openai_entry(config)
    adapter = OpenAILLMProvider(entry, api_key="sk-openai-marker-12345")
    with pytest.raises(ConfigError, match="ai-studio\\[providers\\]"):
        adapter._get_client()


def test_unknown_provider_name_raises_config_error(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    entry = _anthropic_entry(config).model_copy(update={"provider": "bogus"})
    with pytest.raises(ConfigError):
        build_provider(entry, settings=config.settings)


# --------------------------------------------------------------- T-ADP-09


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["anthropic", "openai"])
async def test_request_json_never_contains_api_key(tmp_path, monkeypatch, kind):
    from conftest import build_gateway

    from studio.core.clock import FakeClock
    from studio.core.ids import SequentialIds

    monkeypatch.setenv(
        "ANTHROPIC_API_KEY", "sk-ant-secret-marker-aaaabbbbcccc"
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret-marker-ddddeeeeffff")
    home = make_home(tmp_path, filename=f"secret{kind}")
    monkeypatch.setenv("STUDIO_HOME", str(home))
    config = load_all(home)
    db = Database(home / "data" / "studio.db")
    migrate(db)
    clock = FakeClock()
    ids = SequentialIds("secret")
    if kind == "anthropic":
        client = _FakeAnthropicClient(response=_anthropic_response())
        provider = AnthropicLLMProvider(_anthropic_entry(config), client=client)
        model_key = "llm.creative"
    else:
        client = _FakeOpenAIClient(response=_openai_response())
        provider = OpenAILLMProvider(_openai_entry(config), client=client)
        model_key = "llm.light"
    gw = build_gateway(db=db, config=config, clock=clock, ids=ids,
                       providers={model_key: provider})
    await gw.llm(model_key, _req(), CallContext(episode_id=None, stage_run_id=None,
                                                nonce="n1", purpose="t-adp-09"))
    rows = db.connect().execute("SELECT request_json FROM provider_calls").fetchall()
    assert rows, "expected at least one provider_calls row"
    for row in rows:
        assert "sk-ant-secret-marker" not in row["request_json"]
        assert "sk-openai-secret-marker" not in row["request_json"]


def test_prices_unchanged_on_shipped_registry(monkeypatch):
    monkeypatch.setenv("STUDIO_HOME", str(REPO_ROOT))
    config = load_all(REPO_ROOT)
    creative = config.registry.models["llm.creative"]
    light = config.registry.models["llm.light"]
    assert creative.model_id == "claude-sonnet-4-5-20250929"
    assert light.model_id == "gpt-4o-mini"
    assert (float(creative.pricing.in_usd), float(creative.pricing.out_usd)) == (3.0, 15.0)
    assert (float(light.pricing.in_usd), float(light.pricing.out_usd)) == (0.15, 0.6)
    assert str(creative.pricing_verified_on) == "2026-11-09"
    assert str(light.pricing_verified_on) == "2026-11-09"
    assert creative.needs_confirmation is False
    assert light.needs_confirmation is False


def test_registry_subset_resolves_but_full_draft_still_refuses(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    subset = config.registry.resolve_llm_subset("draft")
    assert subset.model_keys == {"llm_creative": "llm.light", "llm_light": "llm.light"}
    with pytest.raises(RegistryError):
        config.registry.resolve_profile("draft")
