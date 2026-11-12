# Internal Builder Agent SDK

The builder SDK is a policy-bound facade over AVRONE's existing agent harness.
It deliberately reuses TrinityCore (PLAN/SQUAD/LEAD), the configured LLM
Gateway (Ollama in the intended local profile), Sovereign OS runner, and Eternal
CodeX logs. It is not a second agent runtime.

## Start

Configure the normal AVRONE environment with Ollama:

- AVRONE_PROVIDER=ollama
- AVRONE_LLM_BASE_URL=http://127.0.0.1:11434/v1
- AVRONE_LLM_MODEL=qwen2.5:3b (or another model already pulled into Ollama)

Then call BuilderAgentSDK.open() as an async context manager. The default
policy refuses to start if the active provider is not Ollama.

## Lifecycle

1. build(task, mode="squad") delegates to the existing TrinityCore API.
2. The task is augmented with the AVRONE Control Room build contract.
3. Run history remains in Eternal CodeX; checkpoints pass through to the
   existing Trinity checkpoint implementation.
4. verify(result) executes only operator-allowlisted verification commands
   through the existing Sovereign OS runner.
5. A model response starts as UNVERIFIED. Verification failures become FAIL.
   Passing checks without a concrete Git SHA remain UNVERIFIED.

No Git push, merge, publish, external provisioning, or secret handling is
performed by this SDK. Those actions require separate operator authorization.

## API

- BuilderAgentSDK.open(settings=None, policy=None, extras=True)
- builder.build(task, mode="squad" | "chat" | "deep", workers=None,
  run_id=None, checkpoint=None, dispatch=None, rounds=None, context=None)
- builder.verify(result, commands=None)
- builder.close()
- build_control_room() convenience mission entry point

The default verification commands are compileall and the full pytest suite.
They can be narrowed or expanded only by constructing an explicit
BuilderPolicy. Model output cannot supply verification commands.


## Exact-revision verification

Set BuilderPolicy.expected_sha when the operator knows the exact intended workspace revision. Verification reads git rev-parse HEAD first and refuses to run allowlisted checks when the workspace SHA is absent or differs. This prevents a green test run on a different checkout from being attributed to the intended PR commit.
