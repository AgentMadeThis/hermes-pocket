# Hermes Pocket v3

Public-API native Hermes gateway platform candidate. Source is isolated from the
frozen v1 44-file candidate. **Not released, MIT licensed for first-party Python plugin/host bridge contributions,
not physically qualified, and not an installer.** Independent review remains mandatory before public release.

`register(ctx)` declares an inert `pre_tool_call` hook and the Pocket adapter
factory. Only actual adapter creation in a dedicated profile arms the
unconditional execution veto. Merely installing it does not veto other profiles.
Normal Hermes discovery owns the
adapter, model/provider/session lifecycle and final reply path. No sidecar,
private policy import, direct AIAgent creation or Hermes core patch is shipped.

Only an explicitly dedicated Pocket-only, unrouted, in-process profile is
supported. Never enable in your ordinary profile: all model tool executions in
this home are denied, including unknown task identities. See docs/INSTALL.md.

HMAC secret: `agent.secret_scope.get_secret('POCKET_HMAC_KEY')`, exactly 64
lowercase hex characters representing 32 bytes, never normalized or logged.
Bridge provisioning is independent; BridgeEventQuery accepts explicit key bytes
and never reads Hermes credential files. IPC authentication, owner identity,
time window, durable dedup, Unknown/no-replay and RF reply budgets are retained.

Qualification uses installed Hermes source and committed interpreter, cleared
environment, disposable home, real IPC and dispatcher. HTTP provider responses
are synthetic; no paid model, hardware, RF or audible-output proof is implied.
Qualification evidence is private and outside this source tree; use the
required `--evidence-dir` option. PUBLIC_SOURCE_ALLOWLIST.json is the exact
source-only publication set (plus the allowlist file itself). Original selection
provenance referenced by the retained NOTICE is private and not distributed.

Third-party notices are in third-party-notices. LICENSE grants MIT for our first-party Python plugin/host bridge contributions
with copyright 2026 Hermes Pocket contributors; third-party rights are unchanged. MeshCore serial dependencies and hardware setup
are separate operator-owned prerequisites; this Python candidate ships no
GPL firmware or firmware binary.
