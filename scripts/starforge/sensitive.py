"""Shared credential-key classification for validation and redaction."""
import re
_EXACT = frozenset({"auth", "key", "localstorage", "se", "session", "sessionstorage",
                    "sig", "signedheaders", "sp", "sv"})
_SAFE_METADATA = frozenset({"containssecretvalues", "secretscan"})
_MARKERS = (
    "accesskey", "apikey", "authorization", "authheader", "bearer", "cookie",
    "credential", "passwd", "password", "privatekey", "secret", "signature", "token")
def sensitive_key_name(raw: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", str(raw or "").casefold())
    return normalized not in _SAFE_METADATA and (
        normalized in _EXACT or any(marker in normalized for marker in _MARKERS))
