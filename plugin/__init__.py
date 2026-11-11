"""Hermes plugin entrypoint. Protocol/client imports need no Hermes dependencies."""
def register(ctx):
    from .adapter import register as register_platform
    return register_platform(ctx)
