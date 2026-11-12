"""Optional platform mode: Cognito/mPass SSO with each user's own Chatwoot key.

Enabled only when ``COGNITO_USER_POOL_ID`` is set, and needs the ``[platform]`` extra. The community core reaches it
through one hook in ``__main__``; nothing else in the core imports from here.

This module stays import-light so the hook works without the extra.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

ENABLE_VARIABLE = "COGNITO_USER_POOL_ID"


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return bool(env.get(ENABLE_VARIABLE, "").strip())
