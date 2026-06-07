"""
HMAC-SHA256 secret generation and constant-time signature verification
for webhook triggers.

Each registered webhook has its own secret. Callers send the payload's
HMAC-SHA256 digest in the ``X-Brain-Signature`` header, formatted exactly
like GitHub's ``X-Hub-Signature-256`` so existing recipes (and signing
helpers in popular sender libraries) work without translation.

Header convention
-----------------

::

    X-Brain-Signature: sha256=<hex>

where ``<hex>`` is the lowercase hexadecimal digest of HMAC-SHA256 over
the raw request body using the webhook's secret.

Constant-time discipline
------------------------

``verify_signature`` is written to leak as little timing information as
possible:

* The header is parsed via plain string slicing (no early ``len`` check
  on the hex part — that would let a caller infer the expected length).
* The hex is decoded; if it isn't valid hex, comparison still runs
  against a zero-length expected digest so the failure path takes a
  comparable amount of time.
* Comparison goes through ``hmac.compare_digest``, which short-circuits
  on length mismatch internally but does so in a constant-time-by-byte
  loop within the same length. We do NOT add our own ``len(a) != len(b)``
  branch before calling it — that's the explicit foot-gun the regression
  guard in ``test_hmac.py`` pins against.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import secrets

_SIGNATURE_PREFIX = "sha256="


def generate_secret() -> str:
    """Return a URL-safe random secret suitable for HMAC-SHA256 signing.

    ``secrets.token_urlsafe(32)`` returns ~43 characters of base64url with
    256 bits of entropy. URL-safe means the secret can be passed in a
    shell command or HTTP header without quoting.
    """
    return secrets.token_urlsafe(32)


def _compute_digest(secret: str, body: bytes) -> bytes:
    """Compute the HMAC-SHA256 digest of ``body`` keyed by ``secret``."""
    return _hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()


def verify_signature(secret: str, body: bytes, provided_signature: str) -> bool:
    """Verify ``provided_signature`` against ``HMAC-SHA256(secret, body)``.

    ``provided_signature`` is the full header value, e.g.
    ``"sha256=abc123..."``. Returns ``True`` only on an exact constant-time
    match; ``False`` on any failure (missing/wrong prefix, malformed hex,
    wrong key, length mismatch, type errors).

    Never raises on caller-supplied input. Internal programming errors
    (e.g. ``secret`` not being a string) still propagate.
    """
    expected = _compute_digest(secret, body)

    if not isinstance(provided_signature, str):
        # Compare against the expected digest with an empty byte string so
        # the false path takes a comparable amount of time to the true path.
        return _hmac.compare_digest(expected, b"")

    if not provided_signature.startswith(_SIGNATURE_PREFIX):
        return _hmac.compare_digest(expected, b"")

    provided_hex = provided_signature[len(_SIGNATURE_PREFIX) :]
    try:
        provided_digest = bytes.fromhex(provided_hex)
    except ValueError:
        return _hmac.compare_digest(expected, b"")

    return _hmac.compare_digest(expected, provided_digest)
