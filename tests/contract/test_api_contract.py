"""The committed OpenAPI contract must be valid and match the routes the application serves."""

import copy
import warnings
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Any

import pytest
import yaml
from fastapi import APIRouter, Body, Depends, FastAPI, Header, Query
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from jsonschema.protocols import Validator
from openapi_spec_validator import validate
from pydantic import BaseModel

from sage_bot.api.app import create_app

ROOT = Path(__file__).resolve().parents[2]
CONTRACT: dict[str, Any] = yaml.safe_load((ROOT / "contracts/openapi.yaml").read_text())
METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

# A parameter is identified by (location, name); header names are case-insensitive.
type ParamKey = tuple[str, str]


def operations(spec: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (path, method): operation
        for path, item in spec.get("paths", {}).items()
        for method, operation in item.items()
        if method in METHODS
    }


CONTRACT_OPERATIONS = operations(CONTRACT)


def served_routes(app: FastAPI) -> dict[tuple[str, str], APIRoute]:
    """The application's route table, read in process; the app does not publish a schema.

    The route table is used instead of FastAPI's generated schema because one route serving
    GET and HEAD has a single operation ID there, while the contract names each operation.
    """
    routes: dict[tuple[str, str], APIRoute] = {}
    for route in app.routes:
        assert isinstance(route, APIRoute), f"unexpected non-API route {route!r}"
        assert route.methods is not None, f"route without methods: {route.path}"
        for method in route.methods:
            routes[(route.path, method.lower())] = route
    return routes


SERVED_ROUTES = served_routes(create_app())


def _key(location: str, name: str) -> ParamKey:
    return location, name.lower() if location == "header" else name


def served_operation(route: APIRoute, method: str) -> dict[str, Any]:
    """The operation FastAPI derives for one route, generated in process and never published.

    FastAPI resolves the route's effective inputs here: the endpoint signature and every
    dependency at any depth, whether attached to the endpoint, the route or the application,
    with parameter models expanded and request names applied. Its parameters, request body and
    responses (including the automatic 422) therefore share one source of truth.
    """
    with warnings.catch_warnings():
        # A route serving GET and HEAD gets one operation ID per route; IDs are not read here.
        warnings.filterwarnings("ignore", "Duplicate Operation ID", UserWarning)
        spec = get_openapi(title="served", version="0", routes=[route])
    assert route.path_format in spec["paths"], f"{route.path} is hidden from FastAPI's schema"
    return spec["paths"][route.path_format][method]


def _resolve(spec: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if not isinstance(ref, str):
        return node
    _, _, kind, name = ref.split("/")
    return spec["components"][kind][name]


def _parameters(spec: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[ParamKey, bool]:
    parameters: dict[ParamKey, bool] = {}
    for node in nodes:
        parameter = _resolve(spec, node)
        parameters[_key(parameter["in"], parameter["name"])] = parameter.get("required", False)
    return parameters


def contract_drift(route: APIRoute, spec: dict[str, Any], path: str, method: str) -> list[str]:
    """Differences between a served route and its contract operation; empty when they agree.

    Parameters, request-body presence and the automatic 422 response all derive from the same
    effective inputs.
    """
    drift: list[str] = []
    served = served_operation(route, method)
    served_params = _parameters({}, served.get("parameters", []))
    served_body = "requestBody" in served
    item = spec["paths"][path]
    declared = item[method]
    declared_params = _parameters(
        spec, [*item.get("parameters", []), *declared.get("parameters", [])]
    )
    declared_body = "requestBody" in declared
    for (location, name), required in sorted(served_params.items()):
        state = "required" if required else "optional"
        if (location, name) not in declared_params:
            drift.append(f"parameters: undeclared {location} {name!r} ({state})")
        elif declared_params[(location, name)] != required:
            drift.append(f"parameters: {location} {name!r} is {state} when served")
    for location, name in sorted(declared_params.keys() - served_params.keys()):
        drift.append(f"parameters: declared {location} {name!r} is not served")
    if served_body and not declared_body:
        drift.append("requestBody: served but not declared")
    if declared_body and not served_body:
        drift.append("requestBody: declared but not served")

    served_codes = set(served["responses"])  # includes 422 whenever the route takes input
    declared_codes = set(declared["responses"])
    drift += [f"responses: undeclared {code}" for code in sorted(served_codes - declared_codes)]
    drift += [
        f"responses: declared {code} is not served"
        for code in sorted(declared_codes - served_codes)
    ]
    return drift


def test_contract_is_valid_openapi() -> None:
    validate(CONTRACT)


def test_contract_version_is_the_package_version() -> None:
    assert CONTRACT["info"]["version"] == version("sage-bot")


def test_contract_and_application_declare_the_same_operations() -> None:
    assert set(CONTRACT_OPERATIONS) == set(SERVED_ROUTES)


@pytest.mark.parametrize("key", sorted(CONTRACT_OPERATIONS))
def test_inputs_and_status_codes_match_the_route(key: tuple[str, str]) -> None:
    assert contract_drift(SERVED_ROUTES[key], CONTRACT, *key) == []


@pytest.mark.parametrize(
    "path",
    sorted(
        {p for p, m in CONTRACT_OPERATIONS if m == "head" and (p, "get") in CONTRACT_OPERATIONS}
    ),
)
def test_head_is_served_with_the_get_headers(path: str) -> None:
    client = TestClient(create_app())
    get, head = client.get(path), client.head(path)
    assert head.status_code == get.status_code
    assert head.headers == get.headers
    assert head.content == b""


def _validator(schema: dict[str, Any]) -> Validator:
    return Draft202012Validator({**schema, "components": CONTRACT["components"]})


@pytest.mark.parametrize("key", sorted(CONTRACT_OPERATIONS))
def test_served_responses_conform_to_the_contract(key: tuple[str, str]) -> None:
    path, method = key
    response = TestClient(create_app()).request(method.upper(), path)
    declared = CONTRACT_OPERATIONS[key]["responses"].get(str(response.status_code))
    assert declared is not None, f"{method.upper()} {path} returned undeclared {response}"
    content = declared.get("content", {})
    if method == "head":
        assert not content, "HEAD responses must not declare a body"
    if not content:
        assert response.content == b""
        return
    media_type = response.headers["content-type"]
    assert set(content) == {media_type}
    _validator(content[media_type]["schema"]).validate(response.json())


def test_liveness_schema_is_closed_to_unimplemented_states() -> None:
    liveness = _validator({"$ref": "#/components/schemas/Liveness"})
    assert liveness.is_valid({"status": "ok"})
    assert not liveness.is_valid({"status": "not_ready"})
    assert not liveness.is_valid({"status": "ok", "version": "1"})
    assert not liveness.is_valid({})


# Regression tests: drift detection must see inputs that only dependencies introduce.


def _live_route(app: FastAPI) -> APIRoute:
    [route] = [r for r in app.routes if isinstance(r, APIRoute) and r.path == "/health/live"]
    return route


def _drift(app: FastAPI) -> list[str]:
    return contract_drift(_live_route(app), CONTRACT, "/health/live", "get")


def test_undeclared_optional_nested_dependency_parameter_is_detected() -> None:
    def inner(verbose: bool = False) -> bool:
        return verbose

    def outer(flag: Annotated[bool, Depends(inner)]) -> bool:
        return flag

    app = FastAPI()

    @app.get("/health/live")
    async def live(probe: Annotated[bool, Depends(outer)]) -> dict[str, str]:
        return {"status": "ok"}

    # Invisible on the endpoint's own parameters, and an ordinary request still succeeds.
    assert _live_route(app).dependant.query_params == []
    response = TestClient(app).get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    drift = _drift(app)
    assert "parameters: undeclared query 'verbose' (optional)" in drift
    assert "responses: undeclared 422" in drift


def test_route_and_application_dependencies_count_as_inputs() -> None:
    def tenant(x_tenant: Annotated[str, Header()]) -> str:
        return x_tenant

    async def live() -> dict[str, str]:
        return {"status": "ok"}

    route_level = FastAPI()
    route_level.add_api_route("/health/live", live, dependencies=[Depends(tenant)])
    app_level = FastAPI(dependencies=[Depends(tenant)])
    app_level.add_api_route("/health/live", live)

    expected = "parameters: undeclared header 'x-tenant' (required)"
    assert expected in _drift(route_level)
    assert expected in _drift(app_level)


def test_body_read_by_a_dependency_requires_a_declared_request_body() -> None:
    def payload(note: Annotated[str, Body()]) -> str:
        return note

    app = FastAPI()

    @app.get("/health/live")
    async def live(data: Annotated[str, Depends(payload)]) -> dict[str, str]:
        return {"status": "ok"}

    drift = _drift(app)
    assert "requestBody: served but not declared" in drift
    assert "responses: undeclared 422" in drift


class Paging(BaseModel):
    limit: int = 10
    cursor: str | None = None


class Tenant(BaseModel):
    x_tenant: str


def test_parameter_models_in_dependencies_are_expanded() -> None:
    def paging(page: Annotated[Paging, Query()]) -> Paging:
        return page

    def tenant(owner: Annotated[Tenant, Header()]) -> Tenant:
        return owner

    app = FastAPI()

    @app.get("/health/live")
    async def live(
        page: Annotated[Paging, Depends(paging)], owner: Annotated[Tenant, Depends(tenant)]
    ) -> dict[str, str]:
        return {"status": "ok"}

    drift = _drift(app)
    assert "parameters: undeclared query 'limit' (optional)" in drift
    assert "parameters: undeclared query 'cursor' (optional)" in drift
    assert "parameters: undeclared header 'x-tenant' (required)" in drift


def test_included_routers_are_rejected_rather_than_skipped() -> None:
    router = APIRouter()

    @router.get("/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    app = FastAPI()
    app.include_router(router, prefix="/health")
    with pytest.raises(AssertionError, match="unexpected non-API route"):
        served_routes(app)


def _contract_with(**changes: Any) -> dict[str, Any]:
    """A copy of the contract whose GET /health/live operation has the given fields changed."""
    spec = copy.deepcopy(CONTRACT)
    spec["paths"]["/health/live"]["get"].update(changes)
    return spec


def _liveness_drift(spec: dict[str, Any]) -> list[str]:
    return contract_drift(SERVED_ROUTES[("/health/live", "get")], spec, "/health/live", "get")


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        (
            {"parameters": [{"name": "verbose", "in": "query", "schema": {"type": "boolean"}}]},
            "parameters: declared query 'verbose' is not served",
        ),
        (
            {"requestBody": {"content": {"application/json": {"schema": {}}}}},
            "requestBody: declared but not served",
        ),
        (
            {
                "responses": {
                    **CONTRACT_OPERATIONS[("/health/live", "get")]["responses"],
                    "503": {"description": "unavailable"},
                }
            },
            "responses: declared 503 is not served",
        ),
    ],
)
def test_declared_but_unserved_contract_entries_are_detected(
    changes: dict[str, Any], expected: str
) -> None:
    assert expected in _liveness_drift(_contract_with(**changes))


def test_required_flag_mismatch_is_detected() -> None:
    def tenant(x_tenant: Annotated[str, Header()]) -> str:
        return x_tenant

    app = FastAPI()

    @app.get("/health/live", dependencies=[Depends(tenant)])
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    spec = _contract_with(
        parameters=[{"name": "X-Tenant", "in": "header", "schema": {"type": "string"}}],
        responses={"200": {"description": "ok"}, "422": {"description": "invalid"}},
    )
    assert contract_drift(_live_route(app), spec, "/health/live", "get") == [
        "parameters: header 'x-tenant' is required when served"
    ]


def test_route_hidden_from_fastapis_schema_fails_loudly() -> None:
    app = FastAPI()

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    with pytest.raises(AssertionError, match="hidden from FastAPI's schema"):
        _drift(app)
