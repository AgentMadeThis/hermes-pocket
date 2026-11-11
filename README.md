# Hermes Pocket

A small radio-connected remote for Hermes chat, notes and a home music player.
A handheld sends a short authenticated request to a local companion; a host
bridge hands it to a native Hermes gateway platform. Replies are bounded for a
low-bandwidth link. Music plays on the host's existing audio system, **not on
the handheld**, and music commands never become model prompts.

## What is included

- `plugin/`: native platform registration, authenticated local Unix IPC,
  owner-scoped sessions, notes, optional location snapshot reader and music API
  clients. Hermes owns model/provider/session lifecycle and final replies.
- `integration/host/bridge/`: reusable admission, queue, contact and serial-safety
  components. This is a library, **not a complete standalone bridge service**.
- P1/R1 compatibility plus the final P2/R2 player codec and guarded controller
  operations used by the compact handheld design.
- Offline tests and installed-Hermes qualification harnesses. Test audio files,
  HTTP responses and owner keys are synthetic; no credentials are included.

## Architecture

```text
handheld UI (not distributed) -> authenticated radio -> USB companion
 -> operator-owned bridge -> HMAC Unix IPC -> Pocket native Hermes adapter
   -> chat: normal Hermes gateway/model -> bounded text reply
   -> P2 music: loopback Music API -> fresh observed state -> R2 reply
```

Radio trust depends on the enrolled full contact key, not its display name or a
short ambiguous prefix. The Unix IPC checks peer identity and authentication;
durable journals reject replay and never automatically retry unknown actions.
An accepted local response or transmit submission does not prove radio receipt,
physical touch acceptance, or audible playback.

## Player capabilities

P2 supports two-row revision-qualified library pages, opaque owner-scoped track
selection, title details, play/resume, pause, next, previous, stop, absolute
volume, bounded absolute-seconds seek, and fixed flat/bass/soft EQ. Titles and
observations fit a single 91-byte UTF-8 response. Missing position, duration and
volume stay unknown. Mutations are attempted once and followed by a fresh GET;
uncertainty is reported as Unknown, never silently retried. Selected-file next
and previous stay within the same approved source. Stale catalog tokens,
symlinks, path escapes, unsafe queues and malformed requests fail closed.

The compact device design has explicit library entry, cached two-row paging,
load-more/scroll intent and tap selection. Its firmware/UI is **not bundled**:
this is the compatible host-side source, not a fully reproducible handheld
firmware distribution. See [hardware boundary](docs/HARDWARE_BOUNDARY.md).

## Install and qualify

Start with [INSTALL](docs/INSTALL.md), [DEPENDENCIES](docs/DEPENDENCIES.md),
[PLAYER](docs/PLAYER.md) and [TESTING](docs/TESTING.md). The install unit is the
contents of `plugin/` in a **new dedicated Pocket-only Hermes home**. Do not
install into a general-purpose profile. Operator enrollment, provider setup,
HMAC provisioning, serial SDK and bridge-service wiring remain separate tasks;
there is no one-command hardware installer in this package.

The public registration API declares an inert hook and adapter factory. Actual
activation requires a dedicated unrouted in-process profile and active
registration handles. Once armed, the execution veto denies **all model tools**,
including unknown task scopes. Merely installing an unconfigured plugin does not
veto unrelated work. The HMAC is supplied by the scoped public secret helper;
the bridge receives separately provisioned bytes, never a Hermes credential path.

## Privacy, dependencies and limits

Chat can leave the host through the model/provider chosen by the operator and
may incur charges. Notes, request identities and deduplication state are stored
locally; optional location access reads an operator-configured authenticated
snapshot. Music uses an existing private loopback HTTP API and local approved
files; no music server, media, audio sink IDs or owner library is distributed.
The generic service root/port are configurable, with no private host identifiers.

Qualification runs real installed Hermes APIs, gateway startup/shutdown, HMAC IPC
and synthetic HTTP fixtures. It is not a paid-model, serial/RF, physical-device
or audible-output test. Hardware observations from a different installed build
are not a test of this sanitized host package. External-controller concurrency
and physical acceptance remain explicit limits in [LIMITATIONS](docs/LIMITATIONS.md).

MIT covers first-party Python plugin/bridge contributions. Preserved SDK/Hermes
notices are in `third-party-notices/`; no GPL firmware is relicensed or bundled.
`PUBLIC_SOURCE_ALLOWLIST.json` contains exact source hashes; execution evidence
is kept outside the source tree. Independent privacy/release review of these
exact bytes is required before publication.
