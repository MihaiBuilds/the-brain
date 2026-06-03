"""
Cron expression parsing — a thin wrapper around ``croniter``.

The wrapper exists so the rest of the codebase depends on
``CronExpression`` rather than on a third-party library directly.
If we ever need to swap libraries (or hand-roll the parser), only
this file changes.

Standard 5-field cron only: ``minute hour day-of-month month day-of-week``.
``croniter`` itself accepts 6- and 7-field forms (with seconds / years);
both are explicitly rejected here so the schedule semantics stay
predictable — minute granularity, no surprise sub-minute fires.

All times are interpreted as UTC. Callers that need local-time
scheduling translate at their boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime

from croniter import CroniterBadCronError, croniter


class InvalidCronError(ValueError):
    """Raised when a cron expression cannot be parsed or is not 5 fields."""


class CronExpression:
    """A parsed cron expression that can produce the next fire time after a given moment."""

    def __init__(self, expression: str) -> None:
        self._expression = expression

    @classmethod
    def parse(cls, expression: str) -> CronExpression:
        """Parse and validate ``expression``. Raises ``InvalidCronError`` on bad input."""
        if not isinstance(expression, str):
            raise InvalidCronError(
                f"cron expression must be a string, got {type(expression).__name__}"
            )

        stripped = expression.strip()
        if not stripped:
            raise InvalidCronError("cron expression must not be empty")

        if len(stripped.split()) != 5:
            raise InvalidCronError(
                f"cron expression must have exactly 5 fields "
                f"(minute hour day-of-month month day-of-week); got: {expression!r}"
            )

        try:
            croniter(stripped)
        except (CroniterBadCronError, ValueError) as exc:
            raise InvalidCronError(f"invalid cron expression {expression!r}: {exc}") from exc

        return cls(stripped)

    def next_fire_after(self, moment: datetime) -> datetime:
        """Return the first fire time strictly after ``moment`` (UTC).

        ``moment`` must be timezone-aware. The returned datetime is in UTC.
        """
        if moment.tzinfo is None:
            raise InvalidCronError("next_fire_after requires a timezone-aware datetime")

        moment_utc = moment.astimezone(UTC)
        iterator = croniter(self._expression, moment_utc)
        return iterator.get_next(datetime).astimezone(UTC)

    def __str__(self) -> str:
        return self._expression

    def __repr__(self) -> str:
        return f"CronExpression({self._expression!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CronExpression):
            return NotImplemented
        return self._expression == other._expression

    def __hash__(self) -> int:
        return hash(self._expression)
