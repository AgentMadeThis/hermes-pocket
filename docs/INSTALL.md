# Dedicated-profile operational contract (no installer)

Do not install this candidate in an existing/general-purpose Hermes home.
Create a NEW owner-controlled disposable/dedicated home by the supported Hermes
profile workflow; do not switch the owner's active profile or copy credentials.
This lane created no production profile, service, dependency or configuration.
See DEPENDENCIES.md for the exact plugin install unit and optional bridge closure.
Registration alone is inert in ordinary profiles; adapter creation still refuses
missing/wrong/shared/routed settings. Once armed, stop/unload the dedicated
process before any profile repurposing; no hot shared-profile conversion.

Required configuration in that home (operator supplies actual private paths and
full enrolled owner public keys; invalid example placeholders must not activate):

```yaml
plugins:
  enabled: [pocket-platform]
  isolation: in_process
  entries:
    pocket-platform:
      settings:
        dedicated_pocket_only: true
gateway:
  multiplex_profiles: false
  profile_routes: {}
  platforms:
    pocket:
      enabled: true
      extra:
        socket_dir: /absolute/private/owned/state
        owners: [ENROLLED_64_LOWERCASE_HEX_OWNER]
        home_music: false
        # Optional P2 music setup; existing private local API required:
        home_music_port: 8899
        home_library_root: /srv/pocket/music
        home_source: lounge
```

No other enabled platform, routing or multiplexer is supported. Configure the
normal Hermes gateway model/provider using its supported setup independently;
the plugin must not resolve provider account credentials. Set
POCKET_ALLOWED_USERS to the same exact owners for native gateway authorization.
Provision POCKET_HMAC_KEY through Hermes' supported secret settings in this NEW
home: exactly 32 random bytes as 64 lowercase hex. No key_file config exists.
The same bytes must be independently and explicitly approved/provisioned to the
bridge operator, passed to BridgeEventQuery(directory, key_bytes, owners).
Never hand it a Hermes .env/auth.json path, provider key or active-home resolver.
No bridge secret-file reader or cross-home credential copier is supplied.

Normal plugin location is this home's plugins/pocket-platform (contents of
plugin/). Start using the standard Hermes gateway workflow, not a manual agent
or custom sidecar. No script here performs installation. Removal: stop only the
owned dedicated gateway, remove/disable only its plugin, retain the durable
Unknown/dedup journal until independently reviewed. Never delete replay history
as a repair. Do not migrate a running request to another bridge/profile.

Only installed 0.22.5 was qualified. Empty adapter [] is IGNORED by that runtime;
['no_mcp'] resolves empty (with a warning), and the execution veto remains
mandatory regardless of schema. Requalify native schema/dispatch on upgrade.
