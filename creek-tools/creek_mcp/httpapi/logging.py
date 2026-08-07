"""The operator-facing name for ``/v1``'s request logging (#1074).

An operator configuring log shipping wants one import — "where is the access
log?" — and should not have to know that the implementation is a middleware and
therefore lives under :mod:`creek_mcp.httpapi.middleware`. So this module is the
facade, and it re-exports rather than redefines: a second copy of
:data:`ACCESS_LOGGER_NAME` would be a filter that silently stops matching the
logger it was written for.

The direction of the re-export matters. The constant is *defined* beside the
code that logs with it and imported here, not the other way round, because the
reverse would make the middleware package depend on this one and close an
import cycle.
"""

from __future__ import annotations

from creek_mcp.httpapi.middleware.access_log import (
    ACCESS_LOGGER_NAME,
    AccessLogMiddleware,
)

__all__ = ["ACCESS_LOGGER_NAME", "AccessLogMiddleware"]
