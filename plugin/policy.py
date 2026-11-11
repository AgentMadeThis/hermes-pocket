"""Deferred public-API execution veto for a dedicated Pocket-only process.

Registration is inert in general-purpose/shared profiles. Runtime activation
requires explicit dedication. Once armed, this process must be unloaded/stopped
before repurposing; it remains fail-closed for ALL identities until unload.
"""
from hermes_cli.config import load_config_readonly
from agent.secret_scope import is_multiplex_active, serves_routed_profile

BLOCK_MESSAGE = 'Pocket does not execute model tools'


def dedicated_profile():
    config = load_config_readonly() or {}
    gateway = config.get('gateway') or {}
    plugins = config.get('plugins') or {}
    settings = ((plugins.get('entries') or {}).get('pocket-platform') or {}).get('settings') or {}
    platforms = gateway.get('platforms') or {}
    # Explicit opt-in is necessary even in an otherwise empty home. No routing,
    # other enabled messaging platform, or host isolation is supported.
    return (settings.get('dedicated_pocket_only') is True
            and plugins.get('isolation') == 'in_process'
            and gateway.get('multiplex_profiles') is False
            and not gateway.get('profile_routes')
            and not is_multiplex_active() and not serves_routed_profile()
            and isinstance(platforms, dict)
            and all(isinstance(value, dict) and (name == 'pocket' or value.get('enabled') is False)
                    for name, value in platforms.items()))


class PocketPolicy:
    def __init__(self):
        self.active = False
        self.handle = None
        self.platform_handle = None
        self.closed = False

    def block(self, **kwargs):
        # Merely installed profiles are unaffected. Once dedicated runtime is
        # armed, ALL scopes (including missing/Unknown task IDs) are denied.
        if self.active:
            return {'action': 'block', 'message': BLOCK_MESSAGE}
        return None

    def activate(self):
        if not dedicated_profile():
            raise RuntimeError('Pocket requires an explicitly dedicated unrouted in-process home')
        if (self.closed or self.handle is None or not self.handle.active
                or self.platform_handle is None or not self.platform_handle.active):
            raise RuntimeError('Pocket execution veto or platform registration is not active')
        self.active = True

    def ready(self):
        return (not self.closed and self.active and self.handle is not None and self.handle.active
                and self.platform_handle is not None and self.platform_handle.active
                and dedicated_profile())

    def close(self):
        self.active = False
        self.closed = True


def install(ctx):
    # Declarative, side-effect-free capability registration. Official probes
    # return None handles; runtime activate() alone checks their validity.
    policy = PocketPolicy()
    policy.handle = ctx.register_hook('pre_tool_call', policy.block)
    ctx.on_unload(policy.close)
    return policy
