"""The integration suite's HTTP helper, driven without a server.

The helper imports `httpx` and nothing else, so it loads without the
testcontainers settings `tests/integration/conftest.py` applies at import time.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.integration.transport import (
    MAXIMUM_DELAY_SECONDS,
    RETRY_BUDGET_SECONDS,
    RETRYABLE_STATUS,
    _delay_for,
    get_json,
    graphql,
    request_with_backoff,
)

TOKEN = "not-a-real-token"


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
        **kwargs,
    )


def test_a_429_is_waited_out_rather_than_asserted_through() -> None:
    """The failure this module was written for, run 33845114251."""
    shedding = response(429, json={"data": None, "errors": [{"message": "Server is shedding load; retry later."}]})
    recorder = Recorder(shedding, shedding, response(200, json={"data": {"Branch": []}}))

    answer = drive(recorder)

    assert answer.status_code == 200
    assert len(recorder.calls) == 3, "the helper stopped asking too early"
    assert recorder.waits == [1.0, 2.0], f"the schedule was {recorder.waits}"


def test_the_server_saying_how_long_beats_the_schedule() -> None:
    """`Retry-After` is the server's reading of its own load, so it wins."""
    recorder = Recorder(response(429, json={}, headers={"Retry-After": "12"}), response(200, json={"data": {}}))

    drive(recorder)

    assert recorder.waits == [12.0], "the schedule was used where the server had answered"


@pytest.mark.parametrize("header", ["Wed, 21 Oct 2015 07:28:00 GMT", "not-a-number", "-3"])
def test_a_retry_after_this_module_cannot_read_falls_back_to_the_schedule(header: str) -> None:
    recorder = Recorder(response(429, json={}, headers={"Retry-After": header}), response(200, json={"data": {}}))

    drive(recorder)

    assert recorder.waits == [1.0], f"{header!r} was read as a delay when it should not have been"


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
    assert len(recorder.calls) == 1, "a 4xx that is not 429 was retried"
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
    recorder = Recorder(response(429, json={}))

    with pytest.raises(AssertionError) as failure:
        drive(recorder, budget=60.0)

    message = str(failure.value)
    assert "did not answer within 60s" in message, message
    assert "last was HTTP 429" in message, message
    assert sum(recorder.waits) == pytest.approx(60.0), f"the helper waited {sum(recorder.waits)}s of a 60s budget"


def test_the_last_wait_is_trimmed_to_what_is_left_of_the_budget() -> None:
    recorder = Recorder(response(429, json={}))

    with pytest.raises(AssertionError):
        drive(recorder, budget=5.0)

    assert sum(recorder.waits) <= 5.0, f"waited {sum(recorder.waits)}s against a 5s budget"


def test_the_schedule_doubles_and_then_holds_at_the_ceiling() -> None:
    assert [_delay_for(n) for n in range(1, 8)] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    assert max(_delay_for(n) for n in range(1, 40)) == MAXIMUM_DELAY_SECONDS


def test_the_budget_outlasts_a_branch_recomputation() -> None:
    """The figure is sized against the backlog, so it is asserted against it."""
    assert RETRY_BUDGET_SECONDS >= 165.0, "the budget no longer outlasts the backlog it was picked for"


def test_the_retryable_set_is_the_busy_ones_and_not_the_wrong_ones() -> None:
    assert 429 in RETRYABLE_STATUS
    assert RETRYABLE_STATUS.isdisjoint({400, 401, 403, 404, 409, 422})


def test_the_token_reaches_the_server_on_every_attempt() -> None:
    """A retry that drops the header retries as an anonymous caller."""
    recorder = Recorder(response(429, json={}), response(200, json={"data": {}}))

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
