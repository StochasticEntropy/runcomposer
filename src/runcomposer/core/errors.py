"""The error taxonomy (DESIGN.md §9): every failure a caller can cause or
observe is a typed exception carrying a stable machine-readable ``code``.

The types say *what kind* of failure it is; transports decide what that means
for them in exactly one place each (the HTTP layer maps a type to a status
code, the CLI to an exit code). Nothing downstream ever inspects a message
text to classify an error.
"""

from __future__ import annotations

__all__ = [
    "PARAMETER_PROBLEMS",
    "AuthRequired",
    "ConfigError",
    "Conflict",
    "DispatchCancelled",
    "DispatchQueueFull",
    "DispatchRefused",
    "Forbidden",
    "IngestError",
    "InvalidParameter",
    "InvalidRequest",
    "InvalidTransition",
    "NotFound",
    "ParseError",
    "PayloadTooLarge",
    "PluginError",
    "RunActive",
    "RunComposerError",
    "RunnerUnavailable",
    "SpecConflict",
    "StoreError",
    "UnknownRun",
    "UnknownRunner",
]


class RunComposerError(Exception):
    """Base of every error this package raises on purpose."""

    code = "error"
    #: Optional machine-readable context a transport adds to its answer.
    extra: dict[str, object] | None = None


# -- the caller asked for something that cannot be done as asked ---------------


class InvalidRequest(RunComposerError, ValueError):
    """The request itself is wrong: bad input, bad reference, bad value."""

    code = "invalid-request"


class InvalidParameter(InvalidRequest):
    """A per-run runner parameter is unknown, missing, ill-typed — or has a
    value the runner cannot carry out. ``parameter`` names it.

    Two spellings::

        InvalidParameter("parameter 'region' must be …", parameter="region")
        InvalidParameter("region", "no build agent serves 'ap'")

    The second — parameter name, then what is wrong — is the one for a
    runner: raised from ``plan`` or from ``dispatch`` before ``declare``, it
    is answered like a value the declaration refuses (HTTP ``400``
    ``invalid-parameter``, no dispatch left behind); raised after the
    hand-off it fails the dispatch with its message.

    ``problem`` says what kind of refusal it is — one of
    :data:`PARAMETER_PROBLEMS`; ``runner`` (the default) for one a runner
    raised — and ``key``, for a ``mapping`` parameter, which of its names
    is concerned. Both are served beside ``parameter``."""

    code = "invalid-parameter"

    def __init__(
        self,
        message: str,
        detail: str | None = None,
        *,
        parameter: str = "",
        problem: str = "runner",
        key: str | None = None,
    ):
        if detail is not None:
            parameter = parameter or message
            message = f"parameter {message!r}: {detail}"
        super().__init__(message)
        if problem not in PARAMETER_PROBLEMS:
            raise ValueError(f"unknown parameter problem {problem!r} (one of {PARAMETER_PROBLEMS})")
        self.parameter = parameter
        self.problem = problem
        self.key = key
        self.extra = {"problem": problem, **({"key": key} if key is not None else {})}


#: What kind of refusal an :class:`InvalidParameter` is: ``unknown`` (the
#: runner declares no such parameter), ``required`` (missing), ``type`` (the
#: wrong kind of value — a string where an integer is declared, several
#: lines for a ``string``), ``choice`` (not among the choices), ``pattern``
#: (a value does not match ``pattern``), ``key-pattern`` (a mapping's name
#: does not match ``key_pattern``), ``runner`` (the runner refused it).
PARAMETER_PROBLEMS = ("unknown", "required", "type", "choice", "pattern", "key-pattern", "runner")


class UnknownRunner(InvalidRequest):
    """The named runner is not configured under ``runners:``."""

    code = "unknown-runner"


class ParseError(RunComposerError, ValueError):
    """A result artifact is malformed or refused. The ONE error a
    ResultParser raises for bad input, so ingestion can route every such
    bundle the same way."""

    code = "parse-error"


# -- the thing addressed does not exist -----------------------------------------


class NotFound(RunComposerError, LookupError):
    """Something asked for does not exist (HTTP 404)."""

    code = "not-found"


class UnknownRun(NotFound):
    """No run has this id."""

    code = "unknown-run"

    def __init__(self, run_id: object):
        super().__init__(f"unknown run id {run_id!r}")
        self.run_id = run_id


# -- the request is fine, the current state forbids it --------------------------


class Conflict(RunComposerError):
    """The request conflicts with the state of what it addresses (HTTP 409)."""

    code = "conflict"


class RunActive(Conflict):
    """The run already has a dispatch in flight."""

    code = "run-active"


class InvalidTransition(Conflict):
    """A lifecycle transition the state machine does not allow."""

    code = "invalid-transition"


class SpecConflict(Conflict):
    """A document claims the id of a stored run but differs from it."""

    code = "spec-conflict"


class DispatchRefused(Conflict):
    """Raised by a Runner (through ``ctx.refuse``) that declines a dispatch
    before the hand-off — the drift check, a failed readiness hook, an
    unreachable executor. Leaves no dispatch behind."""

    code = "dispatch-refused"


class DispatchCancelled(RunComposerError):
    """Raised by ``ctx.raise_if_cancelled()`` inside a runner once
    cancellation was requested."""

    code = "dispatch-cancelled"


# -- authentication of the write path --------------------------------------------


class DispatchQueueFull(RunComposerError):
    """As many dispatches wait for a free slot (``core.dispatch.max_queued``)
    as this deployment queues: try again later (HTTP 503)."""

    code = "dispatch-queue-full"


class RunnerUnavailable(RunComposerError):
    """A configured runner failed to say what it is (its ``describe()``
    raised): nothing can be planned or dispatched with it until it does
    (HTTP 503). The message names the runner, never the exception's text —
    that is in the server log."""

    code = "runner-unavailable"


class AuthRequired(RunComposerError):
    """A token is needed and none was sent (HTTP 401)."""

    code = "token-required"


class Forbidden(RunComposerError):
    """The token sent does not grant this request (HTTP 403)."""

    code = "token-rejected"


class PayloadTooLarge(RunComposerError):
    """An upload is over the configured size limit (HTTP 413)."""

    code = "payload-too-large"


# -- results that cannot be taken in ----------------------------------------------


class IngestError(RunComposerError, ValueError):
    """A results bundle is refused. ``reason`` classifies the refusal so
    transports can route it: unsolicited | unknown-run | sha-mismatch |
    marker-conflict | bad-bundle."""

    code = "ingest-refused"

    def __init__(self, message: str, *, reason: str = "bad-bundle"):
        super().__init__(message)
        self.reason = reason
        self.code = reason


# -- the deployment is wrong -------------------------------------------------------


class ConfigError(RunComposerError, ValueError):
    """Invalid configuration — raised loudly, at startup."""

    code = "config-error"


class PluginError(ConfigError):
    """A declared plugin cannot be loaded."""

    code = "plugin-error"


class StoreError(RunComposerError, RuntimeError):
    """A store-level contract violation."""

    code = "store-error"
