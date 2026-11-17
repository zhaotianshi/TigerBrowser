"""Lazy access to credentials held by the native operating-system keyring."""

from typing import Optional, Protocol


class SecretStoreError(RuntimeError):
    """A credential operation could not be completed securely."""


class SecretStore(Protocol):
    def get(self, key: str) -> Optional[str]: ...

    def set(self, key: str, value: str) -> None: ...

    def delete(self, key: str) -> None: ...


class KeyringSecretStore:
    """Require a native backend; never accept plaintext or null keyrings."""

    _BACKENDS = {
        ("keyring.backends.Windows", "WinVaultKeyring"),
        ("keyring.backends.macOS", "Keyring"),
        ("keyring.backends.SecretService", "Keyring"),
        ("keyring.backends.kwallet", "DBusKeyring"),
        ("keyring.backends.kwallet", "DBusKeyringKWallet4"),
        ("keyring.backends.kwallet", "DBusKeyringKWallet6"),
    }

    def __init__(self, namespace: str):
        self.namespace = namespace
        self._backend = None

    def _native_backend(self):
        if self._backend is None:
            try:
                import keyring

                backend = keyring.get_keyring()
                kind = (type(backend).__module__, type(backend).__name__)
                if kind not in self._BACKENDS or backend.priority <= 0:
                    raise SecretStoreError(
                        "Configure a native Windows Credential Manager, macOS Keychain, "
                        "Secret Service or KWallet backend before saving proxy passwords."
                    )
                self._backend = backend
            except SecretStoreError:
                raise
            except Exception:
                raise SecretStoreError(
                    "The operating-system credential store is unavailable. "
                    "Install the studio extra and unlock the system keyring."
                ) from None
        return self._backend

    def _call(self, operation: str, key: str, *args):
        backend = self._native_backend()
        try:
            # A separate service per credential avoids WinVault's user collision migration.
            return getattr(backend, operation)(f"{self.namespace}/{key}", "proxy", *args)
        except Exception:
            raise SecretStoreError("The operating-system credential operation failed.") from None

    def check_backend(self) -> None:
        """Validate the native backend without reading or writing a credential."""
        self._native_backend()

    def get(self, key: str) -> Optional[str]:
        return self._call("get_password", key)

    def set(self, key: str, value: str) -> None:
        self._call("set_password", key, value)

    def delete(self, key: str) -> None:
        if self.get(key) is not None:
            self._call("delete_password", key)
