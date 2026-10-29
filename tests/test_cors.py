import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.delivery.cors import configure_cors


@pytest.mark.asyncio
async def test_cors_allows_credentialed_dashboard_dataset_requests():
    origin = "http://localhost:3000"
    app = FastAPI()
    app.add_api_route("/datasets", lambda: {"ok": True}, methods=["GET", "PATCH"])
    configure_cors(app, origin)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        preflight = await client.options(
            "/datasets",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "PATCH",
                "Access-Control-Request-Headers": "authorization,if-match",
            },
        )
        actual = await client.get("/datasets", headers={"Origin": origin})

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == origin
    assert preflight.headers["access-control-allow-credentials"] == "true"
    assert "patch" in preflight.headers["access-control-allow-methods"].lower()
    allowed_headers = preflight.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed_headers
    assert "if-match" in allowed_headers
    assert actual.headers["access-control-allow-origin"] == origin
    assert actual.headers["access-control-allow-credentials"] == "true"


@pytest.mark.asyncio
async def test_cors_does_not_allow_unconfigured_origin():
    app = FastAPI()
    app.add_api_route("/datasets", lambda: {"ok": True}, methods=["GET"])
    configure_cors(app, "https://dashboard.example.com")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/datasets", headers={"Origin": "https://untrusted.example"})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_cors_rejects_wildcard_origin():
    with pytest.raises(ValueError, match="explicit origins"):
        configure_cors(FastAPI(), "https://dashboard.example.com, *")
