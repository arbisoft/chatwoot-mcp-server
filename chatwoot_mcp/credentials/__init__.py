"""Per-request Chatwoot credential resolution.

- ``base.py``: the :class:`CredentialProvider` protocol and :class:`ChatwootCredential`.
- ``static.py``: the ``CHATWOOT_HTTP_TOKEN`` environment key.
- ``header.py``: a per-request ``X-Chatwoot-Token`` header on ``/http/api-key/mcp``.
"""

from chatwoot_mcp.credentials.base import ChatwootCredential, CredentialProvider, Profile, token_identity
from chatwoot_mcp.credentials.header import HeaderCredentialProvider, MountRoutedCredentialProvider, api_key_mount
from chatwoot_mcp.credentials.static import StaticCredentialProvider

__all__ = [
    "ChatwootCredential",
    "CredentialProvider",
    "HeaderCredentialProvider",
    "MountRoutedCredentialProvider",
    "Profile",
    "StaticCredentialProvider",
    "api_key_mount",
    "token_identity",
]
