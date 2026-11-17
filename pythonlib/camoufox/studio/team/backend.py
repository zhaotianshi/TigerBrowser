"""Choose a native host backend explicitly; unsupported sessions never fall back."""

def select():
    from .kasm import KasmSession, prerequisites
    prerequisites()
    return KasmSession
