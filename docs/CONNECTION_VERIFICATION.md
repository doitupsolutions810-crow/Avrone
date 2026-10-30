# AVRONE connection verification

 
The verification command distinguishes configuration readiness from a real provider round-trip.

## Usage

```bash
# Configuration-only: no network request and no API quota consumed.
avrone-verify

# Make one small inference request to the configured primary provider.
# This consumes a small amount of provider quota.
avrone-verify --live

# Save the same secret-free report for CI or audit storage.
avrone-verify --live --json connection-report.json
```

Exit code is 0 when the requested scope passes and 1 when configuration or a requested live probe fails. A configuration-only pass does **not** mean the provider is reachable: its report sets `fully_verified=false`. A live probe targets the primary provider only, so a configured fallback cannot mask a broken primary. Fallback names are checked statically but are not contacted by this command.

## What is checked

- Provider name and endpoint URL structure.
- HTTPS for cloud providers that use bearer credentials.
- Model, execution mode, required cloud-key presence, retry count, timeout, and backoff.
- Fallback provider names.
- A non-empty primary model response when `--live` is selected.

The report records status, a bounded error category, route labels, and response length—not the model response, API key, endpoint URL, or provider's raw error body. Keep the environment containing `AVRONE_LLM_API_KEY` or a provider-specific key protected. Never commit credentials or the generated report if it contains deployment details from additional customizations.

This command verifies AVRONE's configured model-provider connection only. It does not claim that sibling services, GitHub Actions runners, Runtime tenants, or Capability Plane credentials are connected. Those require their own contract and end-to-end checks.
