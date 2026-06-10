"""Shared hermetic test fixtures.

Real subprocesses with predictable behavior — used by tests that need
to exercise process-level lifecycles (spawn, stdin/stdout/stderr,
signals, crash recovery) without depending on external services.
"""
