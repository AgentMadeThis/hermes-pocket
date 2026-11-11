# Third-party source attribution

This notice covers third-party material and dependencies only. It does not select or grant a license for Hermes Pocket's original contributions.

## MeshCore Python SDK adaptations — MIT

The host bridge safety adapters (`integration/host/bridge/sdk_safety.py` and `serial_safety.py`) use and adapt implementation patterns from MeshCore Python SDK 2.3.17, including the event dispatcher, message-reader integration, MeshCore constructor wiring and serial connection integration. Local bounded queues, validation and lifecycle changes are Hermes Pocket modifications; upstream endorsement is not implied.

Upstream: https://github.com/meshcore-dev/meshcore_py
Pinned revision: `105f407b85b19709ffff8616d3bf1e83b79c0b9a` (v2.3.17).

Copyright (c) 2025 Florent de Lamotte.

The complete upstream MIT copyright/permission/disclaimer text is preserved in `meshcore-python-MIT.txt`. Retain it with copies or substantial portions of the adapted source.

## Hermes Agent runtime dependency — MIT

The plugin imports Hermes Agent APIs and subclasses its platform adapter. No Hermes core package is vendored in this plugin-only snapshot. A dependency courtesy copy of its pinned MIT text is included as `hermes-agent-MIT.txt`.

Upstream: https://github.com/NousResearch/hermes-agent
Reviewed revision: `ee7e0f517232fb619c72e49d7e648436bddc598e`.

Copyright (c) 2025 Nous Research.

## Separately supplied radio firmware

WADAMESH firmware and MeshCore/meshcomod firmware source, images, libraries, fonts and assets are not included in the plugin-only snapshot. Their respective licenses continue to govern separately acquired or distributed copies. This notice is not a firmware redistribution package or a corresponding-source offer.
