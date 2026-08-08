"""The operator-facing names for ``/v1``'s two logs (#1074, #1122).

An operator configuring log shipping wants one import — "where are the logs?" —
and should not have to know that the implementations are middlewares and
therefore live under :mod:`creek_mcp.httpapi.middleware`. So this module is the
facade, and it re-exports rather than redefines: a second copy of
:data:`ACCESS_LOGGER_NAME` would be a filter that silently stops matching the
logger it was written for.

**There are two, and they are separate on purpose.**
:data:`ACCESS_LOGGER_NAME` carries one structured line per request — five safe
fields and no sixth. :data:`ERROR_LOGGER_NAME` carries the traceback behind a
``500``, which can include whatever the exception's own message included, and
therefore wants its own handler, its own destination and its own retention. One
logger for both would force the safer stream to be handled at the riskier one's
classification.

The direction of the re-export matters. Each constant is *defined* beside the
code that logs with it and imported here, not the other way round, because the
reverse would make the middleware package depend on this one and close an
import cycle.
"""

from __future__ import annotations

from creek_mcp.httpapi.middleware.access_log import (
    ACCESS_LOGGER_NAME,
    AccessLogMiddleware,
)
from creek_mcp.httpapi.middleware.boundary import (
    ERROR_LOGGER_NAME,
    ErrorBoundaryMiddleware,
)

__all__ = [
    "ACCESS_LOGGER_NAME",
    "ERROR_LOGGER_NAME",
    "AccessLogMiddleware",
    "ErrorBoundaryMiddleware",
]
