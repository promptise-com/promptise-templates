"""Each agent key has its own rate limit, so one runaway agent cannot slow the others."""

from __future__ import annotations

from conftest import Harness, error_code


async def test_a_key_over_its_limit_is_slowed_down_alone(tmp_path):
    h = Harness(tmp_path, rate_per_minute=6, rate_burst=3)
    try:
        replies = [await h.call("list_projects", as_="acme-report-bot") for _ in range(4)]
        assert [error_code(r) for r in replies[:3]] == [None, None, None]
        assert error_code(replies[3]) == "RATE_LIMIT_EXCEEDED"
        assert replies[3]["error"]["retryable"] is True

        # Another agent of the same customer still has its own bucket.
        assert error_code(await h.call("list_projects", as_="acme-writer-bot")) is None
    finally:
        await h.http.aclose()
