"""v0.21 Lead: env_report's token-shaped redaction no longer needs a word boundary before the prefix."""
import pytest

from avrone.tools_introspect import redact_value


@pytest.mark.parametrize("value", [
    "xxxxxsk-ZZZZZZZZZZZZZZZZZZZZ", "prefixghp_abcdefghijklmnopqrstu", "idAKIAABCDEFGHIJKLMNOP",
    "tokeneyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0", "sk-abcdefghijklmnop", "Bearer abc.def",
])
def test_glued_and_plain_tokens_are_redacted(value):
    assert "redacted" in redact_value("AVRONE_SOME_PLAIN_SETTING", value)


@pytest.mark.parametrize("value", ["8080", "ollama", "http://127.0.0.1:11434", "/data/avrone", "allow"])
def test_ordinary_values_shown(value):
    assert redact_value("AVRONE_SOME_PLAIN_SETTING", value) == value
