"""Session-wide guards that have to run before pytest collects anything.

There is nothing project-specific here and there should not be. The schema and
object readers live in `tests/unit/conftest.py`, the stack settings in
`tests/integration/conftest.py`. This file exists for one reason: a defect in a
dependency that kills the session before either of those is reached.

`infrahub-testcontainers` registers a pytest plugin through the `pytest11` entry
point, `pytest-infrahub-performance-test`. Its `pytest_sessionstart` builds a
host profile, and that path reaches `psutil.cpu_freq()` with no guard around it
(`plugin.py` -> `performance_test.py::get_system_stats` -> `host.py`). On Apple
Silicon the call raises, and because a `pytest_sessionstart` failure is an
INTERNALERROR the whole run dies before collection.

That includes `tests/unit`, which needs no Docker and no Infrahub: the plugin
loads whenever the package is installed, and `infrahub-testcontainers` is in the
dev group, so every contributor on an arm64 Mac gets nothing from `invoke
test-unit` but a traceback. Upstream already treats the reading as optional --
`host.py` stores it as `cpu_freq.current if cpu_freq else None` -- so the intent
was nullable and only the call site was missed. Remove this once that is fixed
upstream.

Catching `Exception` is deliberate rather than lazy. The failure observed here is
`SystemError: <built-in function cpu_freq> returned a result with an exception
set`, and `SystemError` inherits from `Exception` directly, not from
`RuntimeError` or `OSError`. A tuple naming the plausible-looking errors -- which
is what `infrahub-solution-ai-dc` has -- does not catch it. All three frequency
fields are cosmetic telemetry, so no reading here is worth an INTERNALERROR.
"""

from __future__ import annotations

from typing import Any

try:
    import psutil
except ImportError:
    # Nothing to guard, so say nothing. psutil is not declared in pyproject.toml;
    # it arrives transitively with infrahub-testcontainers, which is also where
    # the plugin patched below comes from. If psutil is gone then so is the
    # plugin, and there is no failure left to prevent. An unguarded import would
    # turn a dependency edge this repository does not own into a collection error
    # for the whole suite, which is the same class of failure this file exists to
    # remove.
    pass
else:
    _original_cpu_freq = psutil.cpu_freq

    def _cpu_freq_or_none(*args: Any, **kwargs: Any) -> Any:
        """Report the CPU frequency, or `None` where the platform cannot.

        Args:
            *args: Passed through to `psutil.cpu_freq`.
            **kwargs: Passed through to `psutil.cpu_freq`.

        Returns:
            Whatever `psutil.cpu_freq` returns, or `None` when it raises.
        """
        try:
            return _original_cpu_freq(*args, **kwargs)
        except Exception:
            return None

    psutil.cpu_freq = _cpu_freq_or_none
