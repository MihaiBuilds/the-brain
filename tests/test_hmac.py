"""Tests for ``src.triggers.hmac`` — HMAC-SHA256 secret generation
and constant-time signature verification.

The timing-attack regression guard at the bottom of this file is
load-bearing: a future "optimization" that adds an early
``len(a) != len(b)`` branch in ``verify_signature`` would let an
attacker probe the expected signature length, and this test pins
against that by measuring wall-clock variance between two failure
shapes.
"""

from __future__ import annotations

import hashlib
import hmac
import statistics
import time

from src.triggers.hmac import generate_secret, verify_signature

# ---------------------------------------------------------------------------
# generate_secret
# ---------------------------------------------------------------------------


def test_generate_secret_returns_url_safe_string():
    secret = generate_secret()
    assert isinstance(secret, str)
    # token_urlsafe alphabet: A-Z, a-z, 0-9, hyphen, underscore.
    assert all(c.isalnum() or c in "-_" for c in secret)


def test_generate_secret_is_unique_across_calls():
    secrets_seen = {generate_secret() for _ in range(50)}
    assert len(secrets_seen) == 50


def test_generate_secret_has_at_least_256_bits_of_entropy():
    # token_urlsafe(32) → 32 random bytes → base64url → ~43 chars.
    # Length is a sanity check that we did not slip to a shorter token.
    secret = generate_secret()
    assert len(secret) >= 40


# ---------------------------------------------------------------------------
# verify_signature — happy path
# ---------------------------------------------------------------------------


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_verify_signature_returns_true_on_correct_signature():
    secret = "the-secret"
    body = b'{"event": "push"}'
    assert verify_signature(secret, body, _sign(secret, body)) is True


def test_verify_signature_returns_true_on_empty_body():
    secret = "the-secret"
    body = b""
    assert verify_signature(secret, body, _sign(secret, body)) is True


def test_verify_signature_returns_true_on_large_body():
    # 64 KiB body — a realistic upper bound for a webhook payload.
    secret = "the-secret"
    body = b"x" * (64 * 1024)
    assert verify_signature(secret, body, _sign(secret, body)) is True


# ---------------------------------------------------------------------------
# verify_signature — rejection branches
# ---------------------------------------------------------------------------


def test_verify_signature_returns_false_on_wrong_secret():
    body = b"payload"
    sig_from_other_key = _sign("other-secret", body)
    assert verify_signature("the-secret", body, sig_from_other_key) is False


def test_verify_signature_returns_false_on_tampered_body():
    secret = "the-secret"
    sig_for_original = _sign(secret, b"payload")
    assert verify_signature(secret, b"tampered", sig_for_original) is False


def test_verify_signature_returns_false_on_missing_prefix():
    secret = "the-secret"
    body = b"payload"
    bare_hex = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(secret, body, bare_hex) is False


def test_verify_signature_returns_false_on_wrong_algorithm_prefix():
    secret = "the-secret"
    body = b"payload"
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(secret, body, f"sha1={digest}") is False


def test_verify_signature_returns_false_on_malformed_hex():
    secret = "the-secret"
    body = b"payload"
    assert verify_signature(secret, body, "sha256=not-actually-hex") is False


def test_verify_signature_returns_false_on_short_signature():
    secret = "the-secret"
    body = b"payload"
    # Truncated hex — still hex-decodable, just wrong length.
    assert verify_signature(secret, body, "sha256=abcd") is False


def test_verify_signature_returns_false_on_non_string_signature():
    secret = "the-secret"
    body = b"payload"
    assert verify_signature(secret, body, None) is False
    assert verify_signature(secret, body, b"sha256=...") is False


# ---------------------------------------------------------------------------
# Timing-attack regression guard
#
# A future "optimization" of verify_signature that adds an early
# ``len(provided) != len(expected)`` branch would let a caller distinguish
# "wrong length" from "right length, wrong value" via response time.
# This test asserts those two failure shapes finish in roughly the same
# wall-clock time, with a generous statistical margin so it does not
# flake under load.
#
# The threshold is intentionally loose — we are pinning against a
# 100x-or-worse timing leak, not microbenchmarking. If this test ever
# fails, the offending change introduced an early length-check branch
# and must be reverted.
# ---------------------------------------------------------------------------


def test_verify_signature_does_not_leak_length_via_timing():
    secret = "the-secret"
    body = b"payload"

    correct_sig = _sign(secret, body)
    # Same length as a correct signature but every byte wrong.
    wrong_value_sig = "sha256=" + ("0" * (len(correct_sig) - len("sha256=")))
    # Truncated to a fraction of the correct length.
    wrong_length_sig = "sha256=abcd"

    def _time_many(sig: str, iterations: int = 2000) -> float:
        # Median of per-call timings — robust to scheduler jitter, GC pauses.
        samples = []
        for _ in range(iterations):
            t0 = time.perf_counter_ns()
            verify_signature(secret, body, sig)
            samples.append(time.perf_counter_ns() - t0)
        return statistics.median(samples)

    # Warm-up — Python's first call into a function does extra setup.
    _time_many(correct_sig, iterations=100)

    median_wrong_value = _time_many(wrong_value_sig)
    median_wrong_length = _time_many(wrong_length_sig)

    # Allow up to 10x variance — a real timing leak would be orders of
    # magnitude (length-check returns in nanoseconds, full compare in
    # microseconds). 10x is the regression-guard threshold; tighter
    # bounds would flake on CI under load.
    ratio = max(median_wrong_value, median_wrong_length) / max(
        min(median_wrong_value, median_wrong_length), 1
    )
    assert ratio < 10.0, (
        "verify_signature appears to leak timing information based on "
        f"signature length: wrong-value median={median_wrong_value}ns, "
        f"wrong-length median={median_wrong_length}ns, ratio={ratio:.2f}x"
    )


# ---------------------------------------------------------------------------
# Round-trip with generate_secret
# ---------------------------------------------------------------------------


def test_generated_secret_round_trips_through_verification():
    secret = generate_secret()
    body = b'{"action": "opened"}'
    sig = _sign(secret, body)
    assert verify_signature(secret, body, sig) is True
