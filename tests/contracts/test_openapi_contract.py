import json
from pathlib import Path

from fastapi.testclient import TestClient

from memory_ops.api import app


IDENTITY_SCHEMAS = {
    "TenantIdentity",
    "WorkspaceIdentity",
    "SharedSpaceIdentity",
    "PrincipalIdentity",
    "AgentIdentity",
    "SubjectIdentity",
    "SessionIdentity",
    "ResourceIdentity",
}


def test_versioned_openapi_endpoint_matches_published_contract() -> None:
    response = TestClient(app).get("/v1/openapi.json")

    assert response.status_code == 200
    assert response.json() == json.loads(Path("openapi/openapi.json").read_text())


def test_contract_publishes_shared_identity_and_error_schemas() -> None:
    document = app.openapi()
    schemas = document["components"]["schemas"]

    assert document["openapi"].startswith("3.1.")
    assert IDENTITY_SCHEMAS <= schemas.keys()
    assert "ErrorResponse" in schemas
    assert schemas["TenantIdentity"]["additionalProperties"] is False
    assert schemas["TenantIdentity"]["properties"]["tenant_id"]["format"] == "uuid"


def test_contract_keeps_product_routes_versioned() -> None:
    product_paths = [path for path in app.openapi()["paths"] if not path.startswith("/health/")]

    assert all(path.startswith("/v1/") for path in product_paths)


def test_contract_exposes_bearer_authorization_for_product_routes() -> None:
    document = app.openapi()

    assert document["components"]["securitySchemes"]["BearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
        "description": "Enter the API token without the 'Bearer ' prefix.",
    }
    for path, path_item in document["paths"].items():
        for operation in path_item.values():
            if isinstance(operation, dict) and "responses" in operation:
                expected = [{"BearerAuth": []}] if path.startswith("/v1/") else None
                assert operation.get("security") == expected
