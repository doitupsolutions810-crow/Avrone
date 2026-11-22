"""Auditable provider-connection preflight for AVRONE.

By default this checks local configuration only and makes no network request.
Use --live to send one minimal, non-tool model request to the configured primary
provider. Reports never include API keys, base URLs, response content, or raw
provider error bodies.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any

import httpx

from .config import PROVIDERS, Settings
from .gateway import Gateway


CLOUD_KEY_PROVIDERS = {"openai", "xai", "openrouter"}
_STATUS_CATEGORIES = {
    401: "authentication_failed",
    403: "permission_denied",
    404: "endpoint_or_model_not_found",
    408: "upstream_timeout",
    409: "upstream_conflict",
    425: "upstream_not_ready",
    429: "rate_limited",
    500: "upstream_server_error",
    502: "upstream_bad_gateway",
    503: "upstream_unavailable",
    504: "upstream_timeout",
}


def _check(check_id: str, status: str, detail: str) -> dict[str, str]:
    return {"id": check_id, "status": status, "detail": detail}


def _configuration_checks(settings: Settings, env: dict[str, str] | None = None) -> list[dict[str, str]]:
    env = os.environ if env is None else env
    checks: list[dict[str, str]] = []

    provider = str(getattr(settings, "provider", "") or "").strip().lower()
    if provider in PROVIDERS:
        checks.append(_check("provider_supported", "pass", "Configured provider is supported."))
    else:
        checks.append(_check("provider_supported", "fail", "Configured provider is not supported."))

    raw_url = str(getattr(settings, "llm_base_url", "") or "")
    try:
        parsed = urlsplit(raw_url)
        url_ok = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
        no_url_credentials = not parsed.username and not parsed.password
        no_url_query = not parsed.query and not parsed.fragment
    except ValueError:
        url_ok = no_url_credentials = no_url_query = False
        parsed = None

    if not url_ok:
        checks.append(_check("endpoint_url", "fail", "Provider endpoint must be a valid HTTP(S) URL."))
    elif not no_url_credentials:
        checks.append(_check("endpoint_url", "fail", "Do not embed credentials in the provider URL; use environment secrets."))
    elif not no_url_query:
        checks.append(_check("endpoint_url", "fail", "Provider URL must not contain a query string or fragment."))
    elif provider in CLOUD_KEY_PROVIDERS and parsed.scheme != "https":
        checks.append(_check("endpoint_url", "fail", "Cloud provider endpoints must use HTTPS."))
    else:
        checks.append(_check("endpoint_url", "pass", "Provider endpoint URL is structurally valid."))

    model = str(getattr(settings, "llm_model", "") or "").strip()
    checks.append(_check(
        "model_configured",
        "pass" if bool(model) else "fail",
        "A model is configured." if model else "A non-empty model name is required.",
    ))

    mode = str(getattr(settings, "llm_mode", "") or "").strip().lower()
    checks.append(_check(
        "execution_mode",
        "pass" if mode in {"live", "mock"} else "fail",
        "Execution mode is supported." if mode in {"live", "mock"} else "Execution mode must be live or mock.",
    ))

    key = str(getattr(settings, "llm_api_key", "") or "")
    if provider in CLOUD_KEY_PROVIDERS:
        if key:
            checks.append(_check("cloud_credential", "pass", "Required cloud credential is configured; value is not displayed."))
        elif mode == "mock":
            checks.append(_check("cloud_credential", "skipped", "Mock mode does not use a cloud credential."))
        else:
            checks.append(_check("cloud_credential", "fail", "Required cloud credential is missing."))
    else:
        checks.append(_check("cloud_credential", "skipped", "This provider does not require a cloud credential under the current configuration."))

    timeout = getattr(settings, "llm_timeout", None)
    retries = getattr(settings, "llm_retries", None)
    backoff = getattr(settings, "llm_backoff", None)
    numeric_ok = (
        isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
        and math.isfinite(timeout) and timeout > 0
        and isinstance(retries, int) and not isinstance(retries, bool) and 1 <= retries <= 10
        and isinstance(backoff, (int, float)) and not isinstance(backoff, bool)
        and math.isfinite(backoff) and 0 <= backoff <= 60
    )
    checks.append(_check(
        "retry_policy",
        "pass" if numeric_ok else "fail",
        "Timeout, retry count, and backoff are within safe bounds."
        if numeric_ok else "Timeout must be positive, retries 1–10, and backoff 0–60 seconds.",
    ))

    raw_fallbacks = (env.get("AVRONE_LLM_FALLBACKS", "") or "").strip()
    invalid_fallbacks: list[str] = []
    if raw_fallbacks:
        for entry in raw_fallbacks.split(","):
            head = entry.strip().partition("|")[0].strip()
            name = head.partition(":")[0].strip().lower()
            if not head or name not in PROVIDERS:
                invalid_fallbacks.append("invalid")
    if invalid_fallbacks:
        checks.append(_check("fallback_configuration", "fail", "One or more fallback provider entries are invalid."))
    elif raw_fallbacks:
        checks.append(_check("fallback_configuration", "pass", "Configured fallback provider names are supported."))
    else:
        checks.append(_check("fallback_configuration", "warn", "No fallback providers are configured; the primary provider is the only route."))

    return checks


def _error_category(exc: Exception) -> str:
    # Inspect only for a numeric HTTP status. Never return exception text or
    # provider response bodies because they can contain sensitive information.
    match = re.search(r"\b(401|403|404|408|409|425|429|500|502|503|504)\b", str(exc))
    if match:
        return _STATUS_CATEGORIES[int(match.group(1))]
    if isinstance(exc, httpx.TimeoutException):
        return "upstream_timeout"
    if isinstance(exc, httpx.TransportError):
        return "network_error"
    return "upstream_error"


async def verify_connections(settings: Settings | None = None, *, live: bool = False) -> dict[str, Any]:
    """Return a secret-free verification report.

    Without live=True this is a configuration preflight only. With live=True,
    sends one tiny request to the primary upstream only (never a fallback) so
    a fallback cannot mask a broken primary connection.
    """
    settings = settings or Settings()
    checks = _configuration_checks(settings)
    config_ok = all(item["status"] not in {"fail"} for item in checks)
    report: dict[str, Any] = {
        "schema_version": 1,
        "scope": "configuration_and_primary_probe" if live else "configuration_only",
        "provider": str(getattr(settings, "provider", "") or ""),
        "mode": str(getattr(settings, "llm_mode", "") or ""),
        "checks": checks,
        "live_probe": {"status": "not_requested", "detail": "No network request was made."},
        "ok": config_ok,
        "fully_verified": False,
    }

    if not live:
        return report

    if not config_ok:
        report["live_probe"] = {"status": "blocked", "reason": "configuration_failed"}
    elif str(getattr(settings, "llm_mode", "") or "").lower() != "live":
        report["live_probe"] = {"status": "blocked", "reason": "mock_mode"}
    else:
        gateway = Gateway(settings)
        # A primary-only probe is intentional: fallback success must not conceal
        # a primary outage or invalid primary credential.
        gateway.upstreams = gateway.upstreams[:1]
        try:
            answer = await gateway.chat(
                [{"role": "user", "content": "Connection check. Reply with OK only."}],
                model=settings.llm_model,
                temperature=0,
            )
            if not isinstance(answer, str) or not answer.strip():
                report["live_probe"] = {
                    "status": "failed",
                    "error_category": "empty_model_response",
                    "route": list(gateway.last_route),
                }
            else:
                report["live_probe"] = {
                    "status": "pass",
                    "route": list(gateway.last_route),
                    "response_characters": len(answer),
                    "detail": "Primary provider returned a non-empty response; response content was not retained.",
                }
        except Exception as exc:  # safe classification; do not expose exception bodies
            report["live_probe"] = {
                "status": "failed",
                "error_category": _error_category(exc),
                "route": list(gateway.last_route),
            }

    report["ok"] = config_ok and report["live_probe"]["status"] == "pass"
    report["fully_verified"] = report["ok"]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="avrone-verify",
        description="Verify AVRONE provider configuration without exposing secrets.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="make one minimal request to the configured primary provider (uses API quota)",
    )
    parser.add_argument("--json", metavar="PATH", help="also write the report to PATH")
    args = parser.parse_args(argv)

    try:
        report = asyncio.run(verify_connections(Settings(), live=args.live))
    except (ValueError, OSError):
        print("avrone-verify: could not load provider configuration.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

    output = json.dumps(report, indent=2, sort_keys=True)
    print(output)
    if args.json:
        try:
            Path(args.json).write_text(output + "\n", encoding="utf-8")
        except OSError:
            print("avrone-verify: could not write the requested report file.", file=sys.stderr)
            return 1
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
