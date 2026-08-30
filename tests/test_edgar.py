import time

from inhouse.edgar import RateLimiter


def test_rate_limiter_spaces_requests():
    limiter = RateLimiter(rate=50.0)  # 20ms apart
    start = time.monotonic()
    for _ in range(5):
        limiter.acquire()
    # Four gaps of 20ms; the first acquire is free.
    assert time.monotonic() - start >= 0.075


def test_rate_limiter_with_zero_rate_does_not_block():
    limiter = RateLimiter(rate=0)
    start = time.monotonic()
    for _ in range(10):
        limiter.acquire()
    assert time.monotonic() - start < 0.05


import pytest

from datetime import date

from inhouse.edgar import EdgarClient, EdgarError


def test_weekend_dates_are_rejected_without_a_request():
    """EDGAR returns 403 for a missing index, so asking costs four retries."""
    client = EdgarClient("Test test@example.com", rate_limit=1000.0)

    def explode(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("no HTTP request should be made for a weekend")

    client.get = explode
    for day in (date(2026, 8, 29), date(2026, 8, 30)):  # Saturday, Sunday
        with pytest.raises(EdgarError, match="business days only"):
            client.daily_index(day)


def test_holiday_403_becomes_a_readable_message():
    client = EdgarClient("Test test@example.com", rate_limit=1000.0)

    def refuse(url, **kwargs):
        # 403 must not be retried here, or a holiday costs four attempts.
        assert 403 not in kwargs.get("retry_statuses", set())
        raise EdgarError(f"{url} -> HTTP 403")

    client.get = refuse
    with pytest.raises(EdgarError, match="market holiday"):
        client.daily_index(date(2026, 7, 3))
