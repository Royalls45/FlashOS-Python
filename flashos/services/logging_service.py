from __future__ import annotations

import os
import re


def redact_sensitive_paths(message: str) -> str:
    profile = os.environ.get("USERPROFILE", "")
    if profile:
        message = re.sub(re.escape(profile), "%USERPROFILE%", message, flags=re.IGNORECASE)
    return message
