"""Round 23: endpoint and config values as people actually paste them. Base URLs with the
full /chat/completions path, no scheme, quotes, or a bare Ollama host; API keys with 'Bearer ',
quotes, or a newline; junk numeric env vars that used to crash at import; .env lines with
`export`, quoted '#', a BOM; malformed URLs that escaped chat() as raw exceptions."""
import asyncio
import os
import subprocess
import sys

import httpx
import pytest

from avrone import gateway
from avrone.config import (Settings, clean_api_key, normalize_base_url, parse_dotenv_line,
                           resolve_provider)


# --- base URL -----------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,provider,want", [
    ("http://127.0.0.1:11434/v1/chat/completions", "ollama", "http://127.0.0.1:11434/v1"),
    ("https://openrouter.ai/api/v1/chat/completions/", "openrouter", "https://openrouter.ai/api/v1"),
    ("http://h:8080/v1/completions", "custom", "http://h:8080/v1"),
    ("http://h:8080/v1///", "custom", "http://h:8080/v1"),
    ('"http://h:8080/v1"', "custom", "http://h:8080/v1"),
    ("  'http://h:8080/v1'  ", "custom", "http://h:8080/v1"),
    ("localhost:11434", "ollama", "http://localhost:11434/v1"),
    ("127.0.0.1:1234/v1", "custom", "http://127.0.0.1:1234/v1"),
    ("192.168.1.20:11434", "custom", "http://192.168.1.20:11434/v1"),
    ("api.x.ai/v1", "xai", "https://api.x.ai/v1"),
    ("http://127.0.0.1:11434", "ollama", "http://127.0.0.1:11434/v1"),
    ("http://127.0.0.1:11434/", "ollama", "http://127.0.0.1:11434/v1"),
    ("http://127.0.0.1:11434/api", "ollama", "http://127.0.0.1:11434/v1"),
    ("http://127.0.0.1:11434/api/chat", "custom", "http://127.0.0.1:11434/v1"),
    ("HTTP://Box:11434/v1", "ollama", "http://Box:11434/v1"),
    ("http://gw:18789/v1", "openclaw", "http://gw:18789/v1"),
    ("http://proxy.local/ollama/v1", "ollama", "http://proxy.local/ollama/v1"),
])
def test_normalize_base_url(raw, provider, want):
    assert normalize_base_url(raw, provider) == want


@pytest.mark.parametrize("raw", ["", "   ", '""', None])
def test_blank_url_falls_back_to_preset(raw):
    _, url, _, _ = resolve_provider("ollama", raw, None, None, {})
    assert url == "http://127.0.0.1:11434/v1"


def test_custom_with_blank_url_still_errors():
    with pytest.raises(ValueError):
        resolve_provider("custom", '  ""  ', None, None, {})


def test_settings_normalises_env_url(monkeypatch):
    monkeypatch.setenv("AVRONE_PROVIDER", "ollama")
    monkeypatch.setenv("AVRONE_LLM_BASE_URL", "localhost:11434/v1/chat/completions")
    assert Settings().llm_base_url == "http://localhost:11434/v1"


def test_quoted_model_unquoted(monkeypatch):
    _, _, model, _ = resolve_provider("ollama", None, '"gpt-oss:120b"', None, {})
    assert model == "gpt-oss:120b"


# --- API key ------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    ("sk-abc", "sk-abc"), ("Bearer sk-abc", "sk-abc"), ("bearer\tsk-abc", "sk-abc"),
    (' "sk-abc" ', "sk-abc"), ("sk-abc\n", "sk-abc"), ("sk-a bc\r\n", "sk-abc"),
    ("bearertoken", "bearertoken"), ("Bearer-x", "Bearer-x"), ("", ""),
])
def test_clean_api_key(raw, want):
    assert clean_api_key(raw) == want


def test_key_from_env_is_cleaned():
    _, _, _, key = resolve_provider("xai", None, None, None, {"XAI_API_KEY": "Bearer xai-123\n"})
    assert key == "xai-123"


# --- numeric env vars ---------------------------------------------------------------------------

@pytest.mark.parametrize("val", ["30s", "", "  ", "abc", "nan", "inf", "'", "1,5"])
def test_junk_numbers_fall_back_instead_of_crashing(monkeypatch, val):
    monkeypatch.setenv("AVRONE_LLM_TIMEOUT", val)
    monkeypatch.setenv("AVRONE_LLM_RETRIES", val)
    s = Settings()
    monkeypatch.delenv("AVRONE_LLM_TIMEOUT"); monkeypatch.delenv("AVRONE_LLM_RETRIES")
    d = Settings()
    assert (s.llm_timeout, s.llm_retries) == (d.llm_timeout, d.llm_retries)


@pytest.mark.parametrize("val,want", [("3", 3), (" 4 ", 4), ('"5"', 5), ("1_0", 10), ("2.0", 2), ("1e1", 10)])
def test_good_ints_parse(monkeypatch, val, want):
    monkeypatch.setenv("AVRONE_LLM_RETRIES", val)
    assert Settings().llm_retries == want


def test_import_survives_junk_env():
    env = dict(os.environ, AVRONE_LLM_TIMEOUT="30s", AVRONE_LLM_RETRIES="", AVRONE_ENV_FILE="/nonexistent")
    r = subprocess.run([sys.executable, "-c", "import avrone.config as c; print(c.settings.llm_retries)"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().isdigit()


# --- .env lines ---------------------------------------------------------------------------------

@pytest.mark.parametrize("line,want", [
    ("A=1", ("A", "1")), ("export A=1", ("A", "1")), ("  export   A = 1 ", ("A", "1")),
    ('A="a #1"', ("A", "a #1")), ("A='x y' # note", ("A", "x y")), ("A=v # note", ("A", "v")),
    ("A=v\t# note", ("A", "v")), ("\ufeffA=1", ("A", "1")), ("A=1\r", ("A", "1")),
    ("A=", ("A", "")), ("A=a=b", ("A", "a=b")), ('A="unterminated', ("A", '"unterminated')),
    ("A=http://h/#frag", ("A", "http://h/#frag")),
    ("# c", None), ("", None), ("noequals", None), ("bad key=1", None), ("=1", None),
])
def test_parse_dotenv_line(line, want):
    assert parse_dotenv_line(line) == want


def test_dotenv_file_non_utf8_and_dir(tmp_path):
    from avrone.config import _load_dotenv
    f = tmp_path / ".env"
    f.write_bytes(b"AVR23_X=\xff\xfeok\nexport AVR23_Y=2\n")
    os.environ.pop("AVR23_X", None); os.environ.pop("AVR23_Y", None)
    try:
        _load_dotenv(f)
        assert os.environ["AVR23_Y"] == "2" and os.environ["AVR23_X"].endswith("ok")
        _load_dotenv(tmp_path)  # a directory named .env: ignored, no crash
    finally:
        os.environ.pop("AVR23_X", None); os.environ.pop("AVR23_Y", None)


# --- malformed URL at request time ---------------------------------------------------------------

def test_invalid_url_becomes_llmerror_no_retry(monkeypatch):
    monkeypatch.setenv("AVRONE_LLM_MODE", "live")
    monkeypatch.setenv("AVRONE_PROVIDER", "custom")
    monkeypatch.setenv("AVRONE_LLM_BASE_URL", "http://h:8080/v1")
    monkeypatch.setenv("AVRONE_LLM_RETRIES", "3")
    monkeypatch.setenv("AVRONE_LLM_BACKOFF", "0")
    monkeypatch.delenv("AVRONE_LLM_FALLBACKS", raising=False)
    calls = []
    def h(r):
        calls.append(1)
        raise httpx.InvalidURL("bad")
    gateway.HTTP_TRANSPORT = httpx.MockTransport(h)
    try:
        g = gateway.Gateway(Settings())
        with pytest.raises(gateway.LLMError, match="bad base URL"):
            asyncio.run(g.chat([{"role": "user", "content": "x"}]))
        assert len(calls) == 1
    finally:
        gateway.HTTP_TRANSPORT = None


def test_legacy_client_maps_invalid_url():
    import inspect
    from avrone import llm
    assert "httpx.InvalidURL" in inspect.getsource(llm.LLM._legacy_chat)
