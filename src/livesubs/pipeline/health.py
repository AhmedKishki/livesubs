"""What is wrong right now, told to the viewers it affects.

The pipeline used to publish ``delayed`` from three places with a flag each, and a failure
that was nobody's flag (a recogniser that had dropped, a translator that was down) published
nothing at all. Here every problem has a key and a scope, and the status each scope shows
follows from which problems are open: ``delayed`` with the first one's detail, or ``live``
when none is.

A scope is ``None`` for a problem that affects everyone - the microphone, the recogniser -
or a target language for one that affects only its readers, so a translator that is down is
news for its own listeners and not for the rest of the room.
"""

from __future__ import annotations

from collections.abc import Callable

from livesubs.events import PipelineStatus

__all__ = ["Health"]

#: ``publish(status, detail, scope)``.
Publish = Callable[[PipelineStatus, "str | None", "str | None"], None]


class Health:
    """Open problems by scope, and the status each scope last reported."""

    def __init__(self, publish: Publish) -> None:
        self._publish = publish
        self._problems: dict[str | None, dict[str, str]] = {}
        self._reported: dict[str | None, tuple[PipelineStatus, str | None]] = {}

    def problem(self, key: str, detail: str, *, scope: str | None = None) -> None:
        """Open a problem, or update its detail."""

        self._problems.setdefault(scope, {})[key] = detail
        self._report(scope)

    def clear(self, key: str, *, scope: str | None = None, detail: str | None = None) -> None:
        """Close a problem. ``detail`` rides on the ``live`` status that follows, if any."""

        problems = self._problems.get(scope)
        if problems is not None:
            problems.pop(key, None)
        self._report(scope, note=detail)

    def clear_scope(self, scope: str) -> None:
        """Close every problem of a scope, for a language whose readers have all gone."""

        if self._problems.pop(scope, None):
            self._report(scope)

    def is_open(self, key: str, *, scope: str | None = None) -> bool:
        return key in self._problems.get(scope, {})

    def open_problems(self) -> dict[str | None, list[str]]:
        """What is wrong right now, by scope, for the operator console."""

        return {scope: list(problems.values()) for scope, problems in self._problems.items() if problems}

    def _report(self, scope: str | None, note: str | None = None) -> None:
        problems = self._problems.get(scope)
        if problems:
            state = (PipelineStatus.DELAYED, next(iter(problems.values())))
        else:
            state = (PipelineStatus.LIVE, note)

        if self._reported.get(scope, (PipelineStatus.LIVE, None)) == state:
            return
        self._reported[scope] = state
        self._publish(state[0], state[1], scope)
