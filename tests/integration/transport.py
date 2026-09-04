"""One HTTP path for the integration suite, with the backoff a loaded stack needs.

A 429 is not handled here. `infrahub_sdk.rate_limit.RateLimitRetryHandler` does
it, on by default in the SDK since 1.23.1, and it reads `Retry-After` in both
RFC 7231 forms where this module only ever read delta-seconds. Every request
below is sent through it.

What is left is what the SDK does not do. `InfrahubClient.execute_graphql`
retries a `ServerNotReachableError` and only when `retry_on_failure` is set,
which is off by default. A 502 raises out of `raise_for_status`, falls past a
handler that knows 401, 403 and 404, and reaches `decode_json`, so HAProxy's
HTML 503 arrives as a JSON decode error rather than a retry. A read timeout is
not caught by that loop at all. Those are this module's job, against a wall
clock.

`test_infrahub.py::test_a_rejected_mutation_is_a_200` must not come through here.
It asserts on the status line, so it has to see the transport.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

import httpx
from infrahub_sdk.rate_limit import RateLimitRetryHandler

RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
"""Statuses that mean ask again.

429 is not one of them. `RATE_LIMIT` has already retried it by the time a
response reaches this loop, so a 429 that gets here is one the SDK gave up on
and asking again would only lengthen a shed the server has now reported twice.
A 4xx is left out for the reason it always was: a malformed query does not
become well formed on the second try.
"""

RETRY_BUDGET_SECONDS = 30.0
"""How long a read may spend waiting, and deliberately short.

It was 180, sized to outlast the recomputation backlog. Run 33863228168 showed
that waiting that long does not help: the suite went from 33:31 to 43:50 and
still failed, and the 502 and 503 count rose from 1163 to 1331, because a retry
is another request against a server that is already shedding load.

So this absorbs a transient shed and nothing more. A stack that is saturated for
half a minute is a stack problem, and waiting it out here hides it while making
it slightly worse.
"""

MAXIMUM_DELAY_SECONDS = 8.0
"""Ceiling on one wait, so a 30 second budget is still several tries."""

RATE_LIMIT_MAX_RETRIES = 6
RATE_LIMIT_BACKOFF_BASE = 0.5
RATE_LIMIT_BACKOFF_MAX = 8.0

RATE_LIMIT = RateLimitRetryHandler(
    max_retries=RATE_LIMIT_MAX_RETRIES,
    backoff_base=RATE_LIMIT_BACKOFF_BASE,
    backoff_max=RATE_LIMIT_BACKOFF_MAX,
)
"""The SDK's 429 retry, sized to this suite's patience rather than to its own.

Its defaults are ten retries under a sixty second ceiling, which can sit for
minutes. That is the same mistake `RETRY_BUDGET_SECONDS` records: outlasting a
shed is not the job. Six retries under an eight second ceiling is at worst 23.5
seconds, inside the budget, and `tests/unit/test_transport.py` holds that.

Exhausting it raises `RateLimitError`, which names the URL and the attempt
count. That is a better failure than this module's timeout message, so it is
left to travel.
"""


def _delay_for(attempt: int) -> float:
    """1, 2, 4, then 8."""
    return min(2.0 ** (attempt - 1), MAXIMUM_DELAY_SECONDS)


def request_with_backoff(
    method: str,
    url: str,
    *,
    token: str,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 60.0,
    budget: float = RETRY_BUDGET_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    send: Callable[..., httpx.Response] | None = None,
    clock: Callable[[], float] = time.monotonic,
    jitter: Callable[[float, float], float] = random.uniform,
    rate_limit: RateLimitRetryHandler = RATE_LIMIT,
) -> httpx.Response:
    """One request, asked again while the server says it is busy.

    Hands back the first response outside `RETRYABLE_STATUS`, whatever it is.
    Judging the body is the caller's job.

    Each attempt goes out through `rate_limit`, so a 429 is waited out by the
    SDK and never reaches the loop below. Each wait this module does take is
    jittered into the top half of its slot, so two readers that met the same
    500 do not come back together and cause the next one. `sleep`, `send`,
    `clock`, `jitter` and `rate_limit` are injected so
    `tests/unit/test_transport.py` can drive the schedule.
    """
    dispatch = send if send is not None else httpx.request
    deadline = clock() + budget
    attempt = 0
    reason = "nothing was attempted"

    def attempt_once() -> httpx.Response:
        return dispatch(
            method,
            url,
            json=json,
            params=params,
            headers={"X-INFRAHUB-KEY": token},
            timeout=timeout,
        )

    while True:
        attempt += 1
        try:
            response = rate_limit.send(send=attempt_once, url=url)
        except httpx.TransportError as error:
            reason = f"{type(error).__name__}: {error}"
        else:
            if response.status_code not in RETRYABLE_STATUS:
                return response
            reason = f"HTTP {response.status_code}"

        remaining = deadline - clock()
        if remaining <= 0:
            raise AssertionError(
                f"{method} {url} did not answer within {budget:g}s: {attempt} attempts, last was {reason}"
            )
        sleep(min(jitter(_delay_for(attempt) / 2, _delay_for(attempt)), remaining))


def graphql(
    address: str,
    document: str,
    branch: str,
    *,
    token: str,
    timeout: float = 180.0,
    **retry: Any,
) -> dict[str, Any]:
    """Run a GraphQL document against one branch and hand back its `data`.

    A 200 carrying `errors` fails and is never retried. Everything in this
    repository reads its errors out of a 200 body, so that is an answer, and
    retrying it would report a data fault as a timeout.
    """
    response = request_with_backoff(
        "POST",
        f"{address}/graphql/{branch}",
        token=token,
        json={"query": document},
        timeout=timeout,
        **retry,
    )
    try:
        payload = response.json()
    except ValueError as error:
        raise AssertionError(
            f"HTTP {response.status_code} from {address}/graphql/{branch} with a body that is not JSON: "
            f"{response.text[:200]!r}"
        ) from error

    assert "errors" not in payload, f"GraphQL errors (HTTP {response.status_code}): {payload['errors']}"
    data: dict[str, Any] = payload["data"]
    return data


def get_json(
    address: str,
    path: str,
    *,
    token: str,
    params: dict[str, Any] | None = None,
    timeout: float = 60.0,
    **retry: Any,
) -> dict[str, Any]:
    """One REST read, on the same terms, failing on any status but 200."""
    response = request_with_backoff(
        "GET",
        f"{address}{path}",
        token=token,
        params=params,
        timeout=timeout,
        **retry,
    )
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    return body
