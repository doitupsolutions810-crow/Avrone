# AVRONE cross-repository integration bridge

Status: **initial integration PR**. This adds a controlled plugin adapter; it does not claim the separate runtime archive, every service, or every UI has already been merged or deployed.

## What the repository review found

| System | Reusable capability found | Integration boundary |
|---|---|---|
| [AVRONE](https://github.com/doitupsolutions810-crow/Avrone) | Python agent orchestration, REST API, plugins/MCP, telemetry/map, fleet, provider routing | Canonical chat/orchestration surface |
| [Grok Bot](https://github.com/doitupsolutions810-crow/grok-bot-) | Node console, real tool loop, browser/terminal, RAG, task board, voice, approvals, traces/ledger, and AVRONE delegation | Separate Node service; use its /api endpoints rather than copying its runtime into the Python package |
| [Control704 Grok Bot Knowledge](https://github.com/doitupsolutions810-crow/control704-grokbot-knowledge) | FastAPI orchestrator/HUD, nested jobs and sub-agents, artifacts, knowledge endpoints | /jobs is the job-creation contract; actual model execution remains governed by that service's ALLOW_AGENT_EXECUTION setting |
| [Capability Plane](https://github.com/doitupsolutions810-crow/capability-plane) | Capability issue/attenuate/verify/execute contract and AttestPipe evidence events | Must retain capability verification and executor-only boundaries; this bridge intentionally does not call /v1/execute |
| [Great Scale](https://github.com/doitupsolutions810-crow/great-scale) | Scale-state endpoint, Great Scale residual/QGT tests, lattice bridge contract | Read scale state only; no automatic state mutation or claim of empirical validation |
| [CycleKernal](https://github.com/doitupsolutions810-crow/CycleKernal) | Simulation, /health, Prometheus /metrics, stored simulation data | Keep internal; bridge excludes per-user histories and mutation endpoints |
| [swarm-vm-runner](https://github.com/doitupsolutions810-crow/swarm-vm-runner) | Coordinator, role workers, Redis, content host, self-hosted runner deployment | Treat as a worker substrate, not an alternative authorization layer |
| [Unified Tool Stack](https://github.com/doitupsolutions810-crow/UnifiedToolStackApplication-utsp-platform) | High-level platform notes | Current README is only a short statement; implementation and deploy contracts need further inspection before wiring production traffic |
| [SEED Platform docs](https://github.com/doitupsolutions810-crow/seed-platform-v10x-docs) | Existing documentation site/build | Documentation/presentation surface, not a runtime dependency |

The Avrone Python repository currently identifies itself as version 0.19.0. The separately built AVRONE Runtime archive line (through 0.82.0) is not the same artifact as that repository's package version. This PR does not misrepresent the runtime archive as having been vendored, tested, or deployed inside the Python package; its next integration step is to define a versioned contract and CI compatibility gate before moving those runtime components across repositories.

## Added tools

The platform_bridge plugin exposes three tools in the existing AVRONE plugin loader:

- platform_bridge_inventory lists supported platform IDs, fixed read-only resources, and whether writes are enabled.
- platform_bridge_read reads a resource from a configured platform. The agent cannot specify a URL, host, arbitrary path, or HTTP method.
- platform_bridge_submit_job can submit a job only to Control704's documented POST /jobs endpoint. It is off by default, and requires AVRONE_PLATFORM_WRITE_ENABLED=true.

Supported resources:

| Target | Resources |
|---|---|
| avrone | status, telemetry, map, runs |
| grokbot | status, runs, governor, agents, ledger |
| control704 | status, services, jobs, tools, knowledge, artifacts |
| great_scale | scale_state |
| cyclekernel | status, metrics |
| capability_plane | openapi |

Only targets present in AVRONE_PLATFORM_TARGETS_JSON can be contacted. Base URLs are operator configuration, not agent input. Redirects are disabled; response output is capped; per-target bearer tokens can be supplied through AVRONE_BRIDGE_<TARGET>_TOKEN. Do not put tokens in this repository. For public/non-local targets, use HTTPS when sending bearer tokens.

## Configure a private, internal deployment

Add only targets that actually run and are reachable from the AVRONE process. For example, on a private Docker network:

    AVRONE_PLATFORM_TARGETS_JSON={"grokbot":"http://grok-bot:3000","control704":"http://control704:9100","great_scale":"http://great-scale:3000","cyclekernel":"http://cyclekernel-monitoring:3000","capability_plane":"http://capability-plane:8443"}
    AVRONE_PLATFORM_WRITE_ENABLED=false

These names and ports are examples for a private network, not claims that the services are running. Set each URL to the corresponding deployed endpoint; omit services not deployed. Do not expose these services or enable job writes on a public interface without authentication and network policy. Control704 job creation is a mutating operation; start with writes disabled and enable them only after reviewing the target's auth and execution gate.

If a target requires bearer authentication, supply the token as an environment secret rather than committing it:

    AVRONE_BRIDGE_CONTROL704_TOKEN=
    AVRONE_BRIDGE_GROKBOT_TOKEN=
    AVRONE_BRIDGE_CAPABILITY_PLANE_TOKEN=

## Safety constraints

1. Only fixed resource paths are callable; the bridge is not an arbitrary HTTP client.
2. Remote output is marked as untrusted data. It must never be treated as agent instructions or authorization.
3. Read requests use GET. The sole write in this first increment is Control704 job creation and it is explicitly opt-in.
4. There is no generic execution bypass for Capability Plane. Calls that execute capabilities must remain inside its own issued/verified capability contract and evidence path.
5. Great Scale's /api/v1/scale/step is not exposed here. Mathematical state changes require a dedicated reviewed adapter and validation contract.
6. Do not route private CycleKernel histories or user identifiers through this bridge. The supported CycleKernel resources are health and aggregate metrics only.

## Verification and remaining gaps

tests/test_platform_bridge.py checks tool registration, unknown target/resource rejection, fixed paths, URL validation, default write denial, Control704 job payload shape, and input bounds. Repository CI is configured to run the test suite on pull requests; this document does not claim the CI has passed until GitHub reports a green result.

The next integration increments should be separate and contract-driven:

1. Wire the AVRONE Runtime v0.82 admission/worker receipts to an explicit mission envelope, preserving tenant authorization, workload admission, lease fencing, receipts, and audit events.
2. Add an authenticated, idempotent adapter for Control704 job status/events so mission state can be reconciled, not merely submitted.
3. Add a capability-scoped execution adapter only after capability issuance/verification and signed receipt round-trips have automated tests.
4. Correlate run IDs and artifact IDs across AVRONE, Grok Bot, Control704, CycleKernel, and Great Scale without merging their databases or erasing each system's authority boundary.
5. Add end-to-end CI with disposable local services and verify deployment configurations separately from unit tests.
6. Connect UI work only after selecting the exact Replit app/account. The currently connected Wix account returned no sites; Adobe assets are an optional design-input layer, not an execution dependency.

This is an integration foundation and reviewable first patch—not a claim that every repo is already one production system.
