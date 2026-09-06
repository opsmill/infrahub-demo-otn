"""The integration suite's HTTP helper, driven without a server.

The helper imports `httpx` and `infrahub_sdk.rate_limit`, neither of which
touches testcontainers, so it loads without the settings
`tests/integration/conftest.py` applies at import time.

A 429 is the SDK's to retry. What is tested here is that this module hands one
over rather than handling it, and that the handler it hands one to is sized to
fit inside `RETRY_BUDGET_SECONDS`.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from infrahub_sdk.exceptions import RateLimitError
from infrahub_sdk.rate_limit import RateLimitRetryHandler

from tests.integration.transport import (
    MAXIMUM_DELAY_SECONDS,
    RATE_LIMIT,
    RATE_LIMIT_MAX_RETRIES,
    RETRY_BUDGET_SECONDS,
    RETRYABLE_STATUS,
    _delay_for,
    get_json,
    graphql,
    request_with_backoff,
)

TOKEN = "not-a-real-token"


def handler(max_retries: int = 3) -> RateLimitRetryHandler:
    """A real SDK handler whose waits are too small to slow a unit test."""
    return RateLimitRetryHandler(max_retries=max_retries, backoff_base=0.001, backoff_max=0.001)


class Recorder:
    """A stand-in for `httpx.request` that answers from a script and logs the waits."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.waits: list[float] = []
        self.now = 0.0

    def send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        answer = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now

    @staticmethod
    def jitter(low: float, high: float) -> float:
        """The top of the slot, so a test can assert on the schedule itself."""
        assert 0 <= low <= high, f"the jitter window is inverted: {low} to {high}"
        return high


def response(
    status: int, *, json: dict[str, Any] | None = None, text: str = "", headers: dict | None = None
) -> httpx.Response:
    request = httpx.Request("POST", "http://stack/graphql/main")
    if json is not None:
        return httpx.Response(status, json=json, headers=headers, request=request)
    return httpx.Response(status, text=text, headers=headers, request=request)


def drive(recorder: Recorder, **kwargs: Any) -> httpx.Response:
    return request_with_backoff(
        "POST",
        "http://stack/graphql/main",
        token=TOKEN,
        sleep=recorder.sleep,
        send=recorder.send,
        clock=recorder.clock,
        jitter=recorder.jitter,
        **kwargs,
    )


def test_a_429_is_waited_out_by_the_sdk_and_never_by_this_module() -> None:
    """The failure this module was written for, run 33845114251, now the SDK's."""
    shedding = response(429, json={"data": None, "errors": [{"message": "Server is shedding load; retry later."}]})
    recorder = Recorder(shedding, shedding, response(200, json={"data": {"Branch": []}}))

    answer = drive(recorder, rate_limit=handler())

    assert answer.status_code == 200
    assert len(recorder.calls) == 3, "the handler stopped asking too early"
    assert recorder.waits == [], "this module waited on a 429 it was supposed to hand over"


def test_a_429_the_sdk_gives_up_on_travels_rather_than_being_retried_again() -> None:
    """Two refusals in a row is the server saying it twice. Asking a third time lengthens the shed."""
    recorder = Recorder(response(429, json={}))

    with pytest.raises(RateLimitError) as failure:
        drive(recorder, rate_limit=handler(max_retries=0))

    assert "http://stack/graphql/main" in str(failure.value), str(failure.value)
    assert len(recorder.calls) == 1
    assert recorder.waits == []


def test_the_sdk_handler_is_sized_to_fit_inside_the_budget() -> None:
    """The SDK defaults to ten retries under a sixty second ceiling and can sit for minutes."""
    worst_case = sum(RATE_LIMIT.compute_backoff(attempt) for attempt in range(RATE_LIMIT_MAX_RETRIES))

    assert worst_case <= RETRY_BUDGET_SECONDS, (
        f"a 429 can hold a read for {worst_case}s of a {RETRY_BUDGET_SECONDS}s budget"
    )
    assert RATE_LIMIT.backoff_max <= MAXIMUM_DELAY_SECONDS, "one 429 wait can outlast one 5xx wait"
    assert RATE_LIMIT.enabled, "the SDK's 429 retry is off and nothing else here covers one"


def test_a_502_from_the_load_balancer_is_retried_and_its_html_never_parsed() -> None:
    """HAProxy answers HTML for a server that has not come back."""
    recorder = Recorder(response(502, text="<html><body>503 Service Unavailable</body></html>"), response(200, json={}))

    assert drive(recorder).status_code == 200
    assert len(recorder.calls) == 2


def test_a_transport_error_is_retried_and_named_when_the_budget_runs_out() -> None:
    recorder = Recorder(httpx.ConnectError("connection refused"))

    with pytest.raises(AssertionError) as failure:
        drive(recorder, budget=10.0)

    assert "ConnectError" in str(failure.value), str(failure.value)
    assert "connection refused" in str(failure.value)


def test_a_400_is_handed_back_rather_than_retried() -> None:
    recorder = Recorder(response(400, json={"errors": [{"message": "Syntax Error"}]}))

    assert drive(recorder).status_code == 400
    assert len(recorder.calls) == 1, "a 4xx was retried"
    assert recorder.waits == []


def test_a_200_carrying_graphql_errors_is_not_retried() -> None:
    """Retrying one would spend the budget and then blame the wrong thing."""
    recorder = Recorder(response(200, json={"data": None, "errors": [{"message": "no such attribute"}]}))

    with pytest.raises(AssertionError) as failure:
        graphql("http://stack", "{ Branch { name } }", "main", token=TOKEN, sleep=recorder.sleep, send=recorder.send)

    assert "no such attribute" in str(failure.value)
    assert len(recorder.calls) == 1, "a GraphQL error was retried"


def test_the_budget_is_wall_clock_and_the_failure_says_what_it_spent() -> None:
    """Six tries at `2 * (attempt + 1)` gave up after 42 seconds. This does not."""
    recorder = Recorder(response(503, text="<html>"))

    with pytest.raises(AssertionError) as failure:
        drive(recorder, budget=60.0)

    message = str(failure.value)
    assert "did not answer within 60s" in message, message
    assert "last was HTTP 503" in message, message
    assert sum(recorder.waits) == pytest.approx(60.0), f"the helper waited {sum(recorder.waits)}s of a 60s budget"


def test_the_last_wait_is_trimmed_to_what_is_left_of_the_budget() -> None:
    recorder = Recorder(response(503, text="<html>"))

    with pytest.raises(AssertionError):
        drive(recorder, budget=5.0)

    assert sum(recorder.waits) <= 5.0, f"waited {sum(recorder.waits)}s against a 5s budget"


def test_the_schedule_doubles_and_then_holds_at_the_ceiling() -> None:
    assert [_delay_for(n) for n in range(1, 6)] == [1.0, 2.0, 4.0, 8.0, 8.0]
    assert max(_delay_for(n) for n in range(1, 40)) == MAXIMUM_DELAY_SECONDS


def test_the_budget_is_short_enough_to_surface_a_saturated_stack() -> None:
    """It was 180 and that hid the problem while adding to it, in run 33863228168."""
    assert RETRY_BUDGET_SECONDS <= 30.0, "a budget this long waits out saturation instead of reporting it"
    assert RETRY_BUDGET_SECONDS >= 4 * MAXIMUM_DELAY_SECONDS / 2, "the budget is too short for the schedule to run"


def test_every_wait_is_jittered_into_the_top_half_of_its_slot() -> None:
    """Two readers that met the same 500 must not come back together."""
    windows: list[tuple[float, float]] = []

    def record(low: float, high: float) -> float:
        windows.append((low, high))
        return low

    recorder = Recorder(response(503, text="<html>"))
    with pytest.raises(AssertionError):
        request_with_backoff(
            "POST",
            "http://stack/graphql/main",
            token=TOKEN,
            budget=20.0,
            sleep=recorder.sleep,
            send=recorder.send,
            clock=recorder.clock,
            jitter=record,
        )

    assert windows[:3] == [(0.5, 1.0), (1.0, 2.0), (2.0, 4.0)], windows
    assert all(low == high / 2 for low, high in windows), "a wait could be jittered to nearly nothing"


def test_the_retryable_set_is_the_busy_ones_and_not_the_wrong_ones() -> None:
    assert 429 not in RETRYABLE_STATUS, "a 429 the SDK gave up on would be asked for a third time"
    assert RETRYABLE_STATUS == frozenset({500, 502, 503, 504})
    assert RETRYABLE_STATUS.isdisjoint({400, 401, 403, 404, 409, 422})


def test_the_token_reaches_the_server_on_every_attempt() -> None:
    """A retry that drops the header retries as an anonymous caller."""
    recorder = Recorder(response(503, text="<html>"), response(200, json={"data": {}}))

    drive(recorder)

    assert [call["headers"]["X-INFRAHUB-KEY"] for call in recorder.calls] == [TOKEN, TOKEN]


def test_get_json_reads_a_rest_endpoint_on_the_same_terms() -> None:
    recorder = Recorder(response(503, text="<html>"), response(200, json={"nodes": {"OtnSite": {}}}))

    body = get_json(
        "http://stack",
        "/api/schema/summary",
        token=TOKEN,
        params={"branch": "main"},
        sleep=recorder.sleep,
        send=recorder.send,
        clock=recorder.clock,
    )

    assert body == {"nodes": {"OtnSite": {}}}
    assert recorder.calls[0]["params"] == {"branch": "main"}
    assert recorder.calls[0]["method"] == "GET"


def test_a_body_that_is_not_json_says_so_with_the_body_in_the_message() -> None:
    recorder = Recorder(response(200, text="<html><body>hello</body></html>"))

    with pytest.raises(AssertionError) as failure:
        graphql("http://stack", "{ Branch { name } }", "main", token=TOKEN, sleep=recorder.sleep, send=recorder.send)

    assert "not JSON" in str(failure.value)
    assert "hello" in str(failure.value), "the message did not carry the body it could not read"
