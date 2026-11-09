"""HTTP application. Liveness reads no settings and contacts no dependency."""

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict


class Liveness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"]


def create_app() -> FastAPI:
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

    return app
