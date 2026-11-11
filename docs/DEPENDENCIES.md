# Dependency and consumer boundaries

The plugin directory is the install unit. Copy only its contents to the NEW
home's `plugins/pocket-platform/`; do not install the repository root as a
plugin. Registration is declarative: missing settings or a capability-probe
context is allowed to describe capabilities, never to create/connect an adapter.
Actual adapter creation, connection and model-turn admission require explicit
dedication plus active hook/platform registration handles. A merely installed
unconfigured/shared profile has an inert hook and cannot create a Pocket adapter.

Once armed, the dedicated process denies every model-tool scope until plugin
unload. Stop/unload it before repurposing; hot conversion into a shared gateway
is unsupported. Missing/Unknown task identity never bypasses an armed veto.

Qualified Hermes baseline: 0.22.5, Python 3.14.9. The plugin uses standard-library
modules and Hermes-provided public gateway/config/secret APIs. No extra plugin
pip dependencies or automatic installations are needed. Older Hermes releases
and future upgrades require requalification, not inferred compatibility.

The optional physical serial bridge is a separate operator-owned library, not
an executable installer/service. It needs meshcore==2.3.17 (Python >=3.10).
Official PyPI version metadata lists runtime dependencies: bleak, pycayennelpp,
pycryptodome, pyserial-asyncio-fast. Those transitives are not locked, vendored,
installed, or native-qualified here. The plugin does not import this SDK. Fake
serial fixtures do not prove SDK availability or physical serial/RF acceptance.
The operator must separately build/qualify an isolated bridge environment and
provide approved key bytes, enrolled contacts and hardware settings. There is
no supplied bridge CLI, credential copier, hardware bootstrap or turnkey setup.

Retained MIT/third-party notices apply only to their named source. No firmware,
MeshCore SDK distribution or transitive package is bundled/relicensed here.
Consumer catalog install and optional physical bridge readiness are separate
claims; a standalone validator pass is not a turnkey-install or hardware pass.
