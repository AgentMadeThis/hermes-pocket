# Security boundary

Only exact full enrolled owner identities, authenticated same-UID private Unix
IPC, fixed timestamp validity, durable at-most-once claims and bounded replies
are accepted. HMAC protocol and dedup/time/budget code are unchanged from v1.
Unknown/ambiguous outcomes never replay automatically, including after restart.

The dedicated-profile registered pre_tool_call veto denies ALL model tools,
including injected MCP/plugin tools and missing/unknown task IDs. Hook errors
and concurrent busy/timeout paths use Hermes' native fail-closed dispatch. The
adapter refuses a turn without an active policy registration. Schema reduction
is defence in depth, never a replacement for dispatch enforcement.

The operator must use a trusted Pocket-only in-process profile with no other
enabled messaging platforms, multiplexing or routes. This is not a sandbox
against an administrator or hostile Python plugins; do not install unreviewed
context engines/plugins or expose this home as a general-purpose tool agent.

Hermes reads only its scoped POCKET_HMAC_KEY public helper. The bridge takes
approved raw bytes directly and never reads Hermes credential stores. Do not
log secrets, copy owner approvals/contacts into code, or expose real owner keys.
Report vulnerabilities privately to the owner; contact selection is pending.
