"""Tests for ``src.scheduler.cron``.

Pure parser tests — no database, no fixtures. The wrapper is tiny; the
point is to lock the contract (5-field only, UTC-only, raises
``InvalidCronError`` on bad input) so the daemon and lifecycle CLI can
depend on it without re-checking each caller.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from src.scheduler import CronExpression, InvalidCronError

# ---------------------------------------------------------------------------
# parse — happy path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expression",
    [
        "* * * * *",
        "*/5 * * * *",
        "0 0 * * *",
        "0 9 * * 1-5",
        "15,45 * * * *",
        "0 0 1 1 *",
        "0 0 29 2 *",
    ],
)
def test_parse_accepts_valid_5_field_expressions(expression: str) -> None:
    cron = CronExpression.parse(expression)
    assert str(cron) == expression


def test_parse_strips_surrounding_whitespace() -> None:
    cron = CronExpression.parse("  */5 * * * *  ")
    assert str(cron) == "*/5 * * * *"


# ---------------------------------------------------------------------------
# parse — rejection of bad input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "   ",
        "not a cron",
        "* * * *",  # 4 fields
        "60 * * * *",  # minute out of range
        "* 24 * * *",  # hour out of range
        "* * 32 * *",  # day-of-month out of range
        "* * * 13 *",  # month out of range
        "* * * * 8",  # day-of-week out of range
        "*/0 * * * *",  # step zero
    ],
)
def test_parse_rejects_invalid_expressions(expression: str) -> None:
    with pytest.raises(InvalidCronError):
        CronExpression.parse(expression)


def test_parse_rejects_6_field_seconds_form() -> None:
    """croniter natively accepts 6 fields (with seconds); we don't."""
    with pytest.raises(InvalidCronError) as exc_info:
        CronExpression.parse("0 * * * * *")
    assert "5 fields" in str(exc_info.value)


def test_parse_rejects_7_field_with_year() -> None:
    """croniter natively accepts 7 fields (with seconds + year); we don't."""
    with pytest.raises(InvalidCronError) as exc_info:
        CronExpression.parse("0 0 * * * * 2026")
    assert "5 fields" in str(exc_info.value)


def test_parse_rejects_non_string_input() -> None:
    with pytest.raises(InvalidCronError):
        CronExpression.parse(None)  # type: ignore[arg-type]
    with pytest.raises(InvalidCronError):
        CronExpression.parse(12345)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# next_fire_after — basic behavior
# ---------------------------------------------------------------------------


def test_next_fire_after_returns_strictly_future() -> None:
    cron = CronExpression.parse("*/5 * * * *")
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    nxt = cron.next_fire_after(base)
    assert nxt > base
    assert nxt == datetime(2026, 6, 1, 12, 5, 0, tzinfo=UTC)


def test_next_fire_after_on_an_exact_fire_moment_skips_to_following() -> None:
    """Exactly on the cron boundary, the next fire is the following one — never the same moment."""
    cron = CronExpression.parse("0 * * * *")  # top of every hour
    on_boundary = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    nxt = cron.next_fire_after(on_boundary)
    assert nxt == datetime(2026, 6, 1, 13, 0, 0, tzinfo=UTC)


def test_next_fire_after_returns_utc() -> None:
    cron = CronExpression.parse("0 * * * *")
    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    nxt = cron.next_fire_after(base)
    assert nxt.tzinfo is not None
    assert nxt.utcoffset() == timedelta(0)


def test_next_fire_after_rejects_naive_datetime() -> None:
    cron = CronExpression.parse("0 * * * *")
    with pytest.raises(InvalidCronError):
        cron.next_fire_after(datetime(2026, 6, 1, 12, 0, 0))


# ---------------------------------------------------------------------------
# next_fire_after — UTC interpretation under DST shifts
# ---------------------------------------------------------------------------


def test_next_fire_after_is_dst_indifferent_when_input_is_utc() -> None:
    """UTC has no DST. A cron fired in UTC must produce evenly-spaced fires
    across the spring-forward and fall-back moments that affect local zones.
    """
    cron = CronExpression.parse("0 * * * *")
    # 2026 EU spring-forward: 2026-03-29 01:00 UTC -> Europe/Bucharest 03:00 → 04:00.
    # The cron should still fire at 02:00 UTC, regardless of any local-tz weirdness.
    spring_forward_window = datetime(2026, 3, 29, 0, 30, 0, tzinfo=UTC)
    nxt = cron.next_fire_after(spring_forward_window)
    assert nxt == datetime(2026, 3, 29, 1, 0, 0, tzinfo=UTC)

    # 2026 EU fall-back: 2026-10-25 01:00 UTC -> Europe/Bucharest 04:00 → 03:00.
    fall_back_window = datetime(2026, 10, 25, 0, 30, 0, tzinfo=UTC)
    nxt = cron.next_fire_after(fall_back_window)
    assert nxt == datetime(2026, 10, 25, 1, 0, 0, tzinfo=UTC)


def test_next_fire_after_converts_non_utc_input_to_utc() -> None:
    """A tz-aware datetime in a non-UTC zone is normalized before scheduling."""
    cron = CronExpression.parse("0 * * * *")
    # 12:30 in a UTC+3 zone == 09:30 UTC; next top-of-hour is 10:00 UTC.
    tz_plus_3 = timezone(timedelta(hours=3))
    nxt = cron.next_fire_after(datetime(2026, 6, 1, 12, 30, 0, tzinfo=tz_plus_3))
    assert nxt == datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# next_fire_after — leap-year correctness
# ---------------------------------------------------------------------------


def test_next_fire_after_handles_feb_29_in_leap_year() -> None:
    """Feb 29 only fires on leap years."""
    cron = CronExpression.parse("0 0 29 2 *")
    # 2028 is a leap year; from 2027-01-01 the next fire is 2028-02-29 00:00.
    base = datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC)
    nxt = cron.next_fire_after(base)
    assert nxt == datetime(2028, 2, 29, 0, 0, 0, tzinfo=UTC)


def test_next_fire_after_skips_non_leap_years_for_feb_29() -> None:
    """From a leap year past Feb 29, the next fire jumps to the next leap year."""
    cron = CronExpression.parse("0 0 29 2 *")
    base = datetime(2028, 3, 1, 0, 0, 0, tzinfo=UTC)
    nxt = cron.next_fire_after(base)
    assert nxt == datetime(2032, 2, 29, 0, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# dunder contract
# ---------------------------------------------------------------------------


def test_str_returns_original_expression() -> None:
    assert str(CronExpression.parse("*/5 * * * *")) == "*/5 * * * *"


def test_repr_is_constructor_shaped() -> None:
    assert repr(CronExpression.parse("0 9 * * 1-5")) == "CronExpression('0 9 * * 1-5')"


def test_equality_by_expression() -> None:
    a = CronExpression.parse("*/5 * * * *")
    b = CronExpression.parse("*/5 * * * *")
    c = CronExpression.parse("*/10 * * * *")
    assert a == b
    assert a != c
    assert a != "*/5 * * * *"  # not equal to a raw string


def test_hashable_by_expression() -> None:
    a = CronExpression.parse("*/5 * * * *")
    b = CronExpression.parse("*/5 * * * *")
    assert hash(a) == hash(b)
    assert {a, b} == {a}
