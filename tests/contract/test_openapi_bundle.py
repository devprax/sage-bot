"""The modular OpenAPI source must bundle into the committed contract, exactly and safely."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]


def _load_bundler() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "bundle_openapi", ROOT / "scripts/bundle_openapi.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bundler = _load_bundler()
BUNDLE = yaml.safe_load((ROOT / "contracts/openapi.yaml").read_text())


def test_committed_bundle_matches_source() -> None:
    assert (ROOT / "contracts/openapi.yaml").read_text() == bundler.render(), (
        "contracts/openapi.yaml is stale; run `make contract`"
    )


def test_bundle_is_self_contained_and_every_ref_resolves() -> None:
    refs = list(bundler.iter_refs(BUNDLE))
    assert refs
    for ref in refs:
        assert ref.startswith("#/components/"), ref
        _, _, kind, name = ref.split("/")
        assert name in BUNDLE["components"][kind], ref


def test_every_source_component_is_published() -> None:
    files = sorted((ROOT / "contracts/openapi/components").rglob("*.yaml"))
    assert files, "no component source files found"
    for file in files:
        kind = "schemas" if file.parent.name == "schemas" else bundler.KINDS[file.name]
        for name in yaml.safe_load(file.read_text()):
            assert name in BUNDLE["components"][kind], f"{file.name}: {name}"


def _tree(tmp_path: Path, files: dict[str, object]) -> Path:
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(content))
    return tmp_path


ROOT_DOC = {"openapi": "3.1.0", "info": {"title": "t", "version": "1"}, "paths": {}}


@pytest.mark.parametrize(
    ("files", "message"),
    [
        (
            {
                "components/schemas/a.yaml": {"X": {"type": "string"}},
                "components/schemas/b.yaml": {"X": {"type": "integer"}},
            },
            "duplicate schemas name 'X'",
        ),
        (
            {"components/schemas/a.yaml": {"X": {"$ref": "#/Missing"}}},
            "unresolved $ref",
        ),
        (
            {"components/schemas/a.yaml": {"X": {"$ref": "../../../outside.yaml#/Y"}}},
            "escapes the source tree",
        ),
        (
            {"components/schemas/a.yaml": {"X": {"$ref": "#/Y/properties"}, "Y": {}}},
            "unsupported $ref",
        ),
        ({"components/schemas/a.yaml": None}, "must be a mapping"),
        ({"paths/a.yaml": {"orphan": {"get": {}}}}, "never bound to a route"),
    ],
)
def test_bundler_rejects_unsafe_sources(
    tmp_path: Path, files: dict[str, object], message: str
) -> None:
    (tmp_path / "outside.yaml").write_text("Y: {}\n")
    source = _tree(tmp_path / "src", {"openapi.yaml": ROOT_DOC, **files})
    with pytest.raises(bundler.BundleError, match=message.replace("$", r"\$")):
        bundler.render(source)


def test_bundler_reports_malformed_yaml_as_bundle_error(tmp_path: Path) -> None:
    source = _tree(tmp_path, {"openapi.yaml": ROOT_DOC})
    (source / "components/schemas").mkdir(parents=True)
    (source / "components/schemas/bad.yaml").write_text("X: [unclosed\n")
    with pytest.raises(bundler.BundleError, match="invalid YAML"):
        bundler.render(source)


def _root(**extra: object) -> dict[str, object]:
    return {**ROOT_DOC, **extra}


SCHEME = {"Bearer": {"type": "http", "scheme": "bearer"}}
SCHEME_FILE = {"components/security-schemes.yaml": SCHEME}


@pytest.mark.parametrize(
    ("root", "message"),
    [
        (
            _root(components={"securitySchemes": {"Inline": {"type": "http", "scheme": "basic"}}}),
            "root components",
        ),
        (
            _root(
                components={
                    "securitySchemes": {
                        "Renamed": {"$ref": "components/security-schemes.yaml#/Bearer"}
                    }
                }
            ),
            "root components",
        ),
        (_root(components={"examples": {}}), "root components"),
        (_root(security=[{"Missing": []}]), "unknown security scheme 'Missing'"),
        (
            _root(paths={"/x": {"get": {"security": [{"Bearer": []}, {"Gone": []}]}}}),
            "unknown security scheme 'Gone'",
        ),
    ],
)
def test_bundler_rejects_unpublished_root_components_and_schemes(
    tmp_path: Path, root: dict[str, object], message: str
) -> None:
    source = _tree(tmp_path, {"openapi.yaml": root, **SCHEME_FILE})
    with pytest.raises(bundler.BundleError, match=message):
        bundler.render(source)


def test_root_component_refs_and_known_schemes_bundle(tmp_path: Path) -> None:
    root = _root(
        security=[{"Bearer": []}],
        components={
            "securitySchemes": {"Bearer": {"$ref": "components/security-schemes.yaml#/Bearer"}}
        },
    )
    doc = yaml.safe_load(bundler.render(_tree(tmp_path, {"openapi.yaml": root, **SCHEME_FILE})))
    assert doc["components"]["securitySchemes"] == SCHEME


def test_literal_ref_shaped_data_is_not_resolved(tmp_path: Path) -> None:
    literal = {"$ref": "#/Other"}
    schemas = {
        "Other": {"type": "string"},
        "RefNamedProperty": {
            "type": "object",
            "properties": {"$ref": {"type": "string"}, "enum": {"$ref": "#/Other"}},
            "required": ["$ref"],
        },
        "LiteralData": {
            "type": "object",
            "enum": [literal],
            "const": literal,
            "default": literal,
            "examples": [literal],
            "x-note": literal,
        },
    }
    source = _tree(tmp_path, {"openapi.yaml": ROOT_DOC, "components/schemas/a.yaml": schemas})
    out = yaml.safe_load(bundler.render(source))["components"]["schemas"]
    assert out["RefNamedProperty"]["properties"] == {
        "$ref": {"type": "string"},
        "enum": {"$ref": "#/components/schemas/Other"},
    }
    assert out["LiteralData"] == schemas["LiteralData"]
    validator = Draft202012Validator(
        {"$ref": "#/components/schemas/LiteralData", "components": {"schemas": out}}
    )
    assert validator.is_valid(literal)


@pytest.mark.parametrize(
    ("root", "message"),
    [
        (_root(security={"Bearer": []}), "security must be a list of mappings"),
        (_root(security=["Bearer"]), "security must be a list of mappings"),
        (_root(webhooks={"w": {}}), "unsupported keyword 'webhooks'"),
        (
            _root(paths={"/x": {"post": {"callbacks": {"c": {}}, "responses": {}}}}),
            "unsupported keyword 'callbacks'",
        ),
        (
            _root(
                paths={
                    "/x": {
                        "get": {
                            "responses": {
                                "200": {
                                    "content": {
                                        "a/b": {
                                            "schema": {
                                                "discriminator": {
                                                    "propertyName": "k",
                                                    "mapping": {"b": "#/B"},
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            ),
            "unsupported keyword 'mapping'",
        ),
    ],
)
def test_bundler_rejects_unsupported_or_malformed_forms(
    tmp_path: Path, root: dict[str, object], message: str
) -> None:
    source = _tree(tmp_path, {"openapi.yaml": root, **SCHEME_FILE})
    with pytest.raises(bundler.BundleError, match=message):
        bundler.render(source)


def test_default_response_and_example_object_refs_resolve(tmp_path: Path) -> None:
    ref = {"$ref": "components/responses.yaml#/Err"}
    media = {
        "examples": {
            "e": {"$ref": "components/schemas/a.yaml#/Lit"},
            "v": {"value": {"$ref": "components/schemas/a.yaml#/Lit"}},
        }
    }
    root = _root(
        paths={"/x": {"get": {"responses": {"default": ref, "200": {"content": {"a/b": media}}}}}}
    )
    source = _tree(
        tmp_path,
        {
            "openapi.yaml": root,
            "components/responses.yaml": {"Err": {"description": "error"}},
            "components/schemas/a.yaml": {"Lit": {"type": "string", "examples": [{"$ref": "#/X"}]}},
        },
    )
    doc = yaml.safe_load(bundler.render(source))
    responses = doc["paths"]["/x"]["get"]["responses"]
    assert responses["default"] == {"$ref": "#/components/responses/Err"}
    assert responses["200"]["content"]["a/b"]["examples"]["e"] == {
        "$ref": "#/components/schemas/Lit"
    }
    assert doc["components"]["schemas"]["Lit"]["examples"] == [{"$ref": "#/X"}]
    assert responses["200"]["content"]["a/b"]["examples"]["v"] == media["examples"]["v"]


def test_non_schema_name_maps_are_not_references(tmp_path: Path) -> None:
    schemas = {
        "Deps": {
            "type": "object",
            "properties": {"$ref": {"type": "string"}, "other": {"type": "string"}},
            "dependentRequired": {"$ref": ["other"]},
        }
    }
    root = _root(
        servers=[{"url": "https://{$ref}", "variables": {"$ref": {"default": "x"}}}],
        security=[{"Bearer": []}],
    )
    source = _tree(
        tmp_path, {"openapi.yaml": root, "components/schemas/a.yaml": schemas, **SCHEME_FILE}
    )
    doc = yaml.safe_load(bundler.render(source))
    assert doc["components"]["schemas"]["Deps"] == schemas["Deps"]
    assert doc["servers"] == root["servers"]


@pytest.mark.parametrize("value", [None, False, {}, "", 0])
@pytest.mark.parametrize("where", ["root", "operation"])
def test_explicitly_malformed_security_is_rejected(
    tmp_path: Path, where: str, value: object
) -> None:
    if where == "root":
        root = _root(security=value)
    else:
        root = _root(paths={"/x": {"get": {"security": value, "responses": {}}}})
    source = _tree(tmp_path, {"openapi.yaml": root, **SCHEME_FILE})
    with pytest.raises(bundler.BundleError, match="security must be a list of mappings"):
        bundler.render(source)


def test_missing_or_empty_security_is_allowed(tmp_path: Path) -> None:
    root = _root(security=[], paths={"/x": {"get": {"responses": {}}}})
    bundler.render(_tree(tmp_path, {"openapi.yaml": root, **SCHEME_FILE}))


def test_iter_refs_yields_only_reference_positions() -> None:
    doc = {
        "paths": {"/x": {"get": {"responses": {"default": {"$ref": "#/components/responses/E"}}}}},
        "components": {
            "schemas": {
                "A": {
                    "type": "object",
                    "properties": {
                        "$ref": {"type": "string"},
                        "b": {"$ref": "#/components/schemas/B"},
                    },
                    "enum": [{"$ref": "#/nowhere"}],
                    "default": {"$ref": "#/nowhere"},
                    "examples": [{"$ref": "#/nowhere"}],
                    "dependentRequired": {"$ref": ["b"]},
                    "x-note": {"$ref": "#/nowhere"},
                }
            }
        },
    }
    assert sorted(bundler.iter_refs(doc)) == [
        "#/components/responses/E",
        "#/components/schemas/B",
    ]


@pytest.mark.parametrize("name", ["default", "value", "enum", "security", "x-note", "$ref"])
def test_iter_refs_walks_components_named_like_keywords(name: str) -> None:
    doc = {
        "components": {
            "schemas": {name: {"$ref": "#/components/schemas/Missing"}},
            "parameters": {name: {"schema": {"$ref": "#/components/schemas/P"}}},
        }
    }
    assert sorted(bundler.iter_refs(doc)) == [
        "#/components/schemas/Missing",
        "#/components/schemas/P",
    ]


@pytest.mark.parametrize("ref", [5, ["x"], True, None])
def test_non_string_ref_is_a_bundle_error(tmp_path: Path, ref: object) -> None:
    source = _tree(
        tmp_path, {"openapi.yaml": ROOT_DOC, "components/schemas/a.yaml": {"X": {"$ref": ref}}}
    )
    with pytest.raises(bundler.BundleError, match=r"\$ref must be a string"):
        bundler.render(source)


@pytest.mark.parametrize("scopes", ["notalist", None, {}, [1]])
def test_security_requirement_scopes_must_be_string_lists(tmp_path: Path, scopes: object) -> None:
    root = _root(security=[{"Bearer": scopes}])
    source = _tree(tmp_path, {"openapi.yaml": root, **SCHEME_FILE})
    with pytest.raises(bundler.BundleError, match="scopes must be a list of strings"):
        bundler.render(source)
