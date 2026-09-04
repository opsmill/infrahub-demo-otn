"""One HTTP path for the integration suite, with the backoff a loaded stack needs.

Neither module retried a 429, which is what took run 33845114251 down: the API
answered `Server is shedding load; retry later.` and the caller asserted through
it. A 429 is the server asking to be called back, which a test can do.

`test_infrahub.py::test_a_rejected_mutation_is_a_200` must not come through here.
It asserts on the status line, so it has to see the transport.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

import httpx

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
"""Statuses that mean ask again. A 4xx that is not 429 is left out: a malformed
query does not become well formed on the second try."""

RETRY_BUDGET_SECONDS = 30.0
"""How long a read may spend waiting, and deliberately short.

It was 180, sized to outlast the recomputation backlog. Run 33863228168 showed
that waiting that long does not help: no test asserted through a 429 any more,
but the suite went from 33:31 to 43:50 and still failed, and the 502 and 503
count rose from 1163 to 1331 because a retry is another request against a server
that is already shedding load.

So this absorbs a transient shed and nothing more. A stack that is saturated for
half a minute is a stack problem, and waiting it out here hides it while making
it slightly worse.
"""

MAXIMUM_DELAY_SECONDS = 8.0
"""Ceiling on one wait, so a 30 second budget is still several tries."""


def _delay_for(attempt: int) -> float:
    """1, 2, 4, then 8."""
    return min(2.0 ** (attempt - 1), MAXIMUM_DELAY_SECONDS)


def _retry_after(response: httpx.Response | None) -> float | None:
    """The server's own delay, when it sent one this module can read.

    Only the delay-seconds form. The header may also carry an HTTP date, and
    guessing at one would wait the wrong length of time without saying so.
    """
    if response is None:
        return None
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


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
) -> httpx.Response:
    """One request, asked again while the server says it is busy.

    Hands back the first response outside `RETRYABLE_STATUS`, whatever it is.
    Judging the body is the caller's job.

    Each wait is jittered into the top half of its slot, so two readers that met
    the same shed do not come back together and cause the next one. `sleep`,
    `send`, `clock` and `jitter` are injected so `tests/unit/test_transport.py`
    can drive the schedule.
    """
    dispatch = send if send is not None else httpx.request
    deadline = clock() + budget
    attempt = 0
    reason = "nothing was attempted"

    while True:
        attempt += 1
        response: httpx.Response | None = None
        try:
            response = dispatch(
                method,
                url,
                json=json,
                params=params,
                headers={"X-INFRAHUB-KEY": token},
                timeout=timeout,
            )
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
        told = _retry_after(response)
        wait = told if told is not None else jitter(_delay_for(attempt) / 2, _delay_for(attempt))
        sleep(min(wait, remaining))


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
