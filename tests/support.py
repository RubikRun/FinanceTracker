"""Assertions that keep synthetic record text out of failure output."""

from __future__ import annotations

import traceback

# Planted in invalid fixtures. It is not a real note, and it must not appear
# in an exception message or traceback.
SYNTHETIC_SECRET = "synthetic-note-do-not-leak-7k2m"


def assert_failure_omits_payload(
    exc: BaseException, secret: str = SYNTHETIC_SECRET
) -> None:
    """Fail if `exc` would show `secret` in a normal traceback.

    The failure message is fixed, so a leak is not copied into the test report.
    """
    rendered = "".join(traceback.format_exception(exc))
    if secret in str(exc) or secret in rendered:
        raise AssertionError(
            f"{type(exc).__name__} included a synthetic payload marker "
            "in its message or traceback"
        )
