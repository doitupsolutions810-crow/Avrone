# AVRONE free preview deployment

This is a **no-paid-compute preview**, not a production deployment.

## What it gives you

- A hosted AVRONE web UI and health endpoint.
- A locked-down preview profile: mock LLM, local runner, no agent shell, no sandbox network, no self-changes, no self-evolution, and no reconnaissance.
- An admin token that you supply privately during Render setup. Protected routes and the event stream require it.
- Automatic deploys from this repository after you connect it to Render.

## Important limits

Render's Free web services sleep after 15 minutes without inbound traffic, take about a minute to wake, and have an ephemeral filesystem. AVRONE run logs, workspace files, and local memory in this profile can disappear on restart, sleep, or redeploy. Render also provides a monthly free instance-hour allowance, not a guaranteed always-on production host.

This profile uses `AVRONE_LLM_MODE=mock`. It verifies the web/API shell and safe orchestration path; it does **not** provide a real LLM brain, durable run history, Docker-backed agent containers, or always-on operation. Do not put customer data or revenue-critical workflows on this preview.

## Deploy steps

1. Open Render and choose **New → Blueprint**.
2. Connect the GitHub repository `doitupsolutions810-crow/Avrone`.
3. Select branch `deploy/render-free-preview`.
4. Review the service plan. It must say **Free**.
5. When prompted for `AVRONE_ADMIN_TOKEN`, create a long random secret in your password manager and enter it directly in Render. Never paste it into chat or commit it to Git.
6. Deploy. If Render offers an upgrade or paid add-on, stop and do not accept it.

## Observe and interact

- Open the deployed service URL. The public `/api/status` endpoint is a read-only health/status view.
- The UI's Terminal tab accepts the admin token locally in your browser for protected operations.
- For analysis in ChatGPT, share the public status output and sanitized logs. Do not share the admin token, API keys, private run content, or customer data.
- The service can sleep and its local data can reset. Export any useful diagnostics before redeploying.

## Promotion gates before production

1. CI passes on the proposed branch.
2. Health check returns HTTP 200.
3. Admin-only routes reject requests without the token and accept the token.
4. WebSocket event stream connects only with admin authorization.
5. Chat, run replay, telemetry, and map endpoints are exercised.
6. Persistence is moved to a durable store and restore is tested.
7. Real model provider is configured with an explicit cost limit.
8. Docker execution, shell, network, fleet, self-change, self-evolution, and recon each receive separate threat review and explicit enablement.
9. A paid always-on host is considered only after a written cost estimate and explicit approval.

A successful free preview is not a production readiness attestation.
