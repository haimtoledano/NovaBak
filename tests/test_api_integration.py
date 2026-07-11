from fastapi.testclient import TestClient


def test_openapi_schema(client: TestClient):
    response = client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    assert "paths" in spec
    assert len(spec["paths"]) > 10


def test_get_hosts_unauthorized(client: TestClient):
    response = client.get("/api/v1/hosts")
    # API key or session required
    assert response.status_code in (401, 403)


# Additional integration tests would require setting up a test user / api key
# in the database, generating a token, and passing it in headers.
