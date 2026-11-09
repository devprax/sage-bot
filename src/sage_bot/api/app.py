"""HTTP application. Liveness reads no settings and contacts no dependency."""

import logging
from typing import Literal

from fastapi import FastAPI, Response
from pydantic import BaseModel, ConfigDict

from sage_bot.api.readiness import ReadinessCheck, not_configured

logger = logging.getLogger(__name__)


class Liveness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"]


class Readiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok", "not_ready"]


def create_app(readiness: ReadinessCheck = not_configured) -> FastAPI:
    # Interactive docs and the schema endpoint are not published by this slice. Slash redirects
    # are off: they answer unknown paths with a Location built from the request's Host header.
    app = FastAPI(
        title="Sage Bot",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )

    # HEAD is accepted because load balancers commonly probe with it.
    @app.api_route("/health/live", methods=["GET", "HEAD"], response_model=Liveness)
    async def live() -> Liveness:
        """Report that the HTTP service is running."""
        return Liveness(status="ok")

    @app.api_route(
        "/health/ready",
        methods=["GET", "HEAD"],
        response_model=Readiness,
        responses={503: {"model": Readiness, "description": "Not ready"}},
    )
    async def ready(response: Response) -> Readiness:
        """Report whether the service's dependencies are available."""
        try:
            available = await readiness()
        except Exception as error:  # probes report state, never errors
            logger.warning("readiness: check raised (%s)", type(error).__name__)
            available = False
        if not available:
            response.status_code = 503
            return Readiness(status="not_ready")
        return Readiness(status="ok")

    return app
