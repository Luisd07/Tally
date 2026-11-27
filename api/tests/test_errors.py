from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.errors import NotFound


def test_unknown_route_uses_error_shape(client: TestClient) -> None:
    r = client.get("/nope")
    assert r.status_code == 404
    body = r.json()["error"]
    assert body["code"] == "not_found"
    assert body["request_id"] == r.headers["X-Request-ID"]


def test_request_id_is_echoed_when_safe(client: TestClient) -> None:
    r = client.get("/healthz", headers={"X-Request-ID": "abc-123"})
    assert r.headers["X-Request-ID"] == "abc-123"


def test_unsafe_request_id_is_replaced(client: TestClient) -> None:
    r = client.get("/healthz", headers={"X-Request-ID": "bad id\twith junk"})
    assert r.headers["X-Request-ID"] != "bad id\twith junk"
    assert len(r.headers["X-Request-ID"]) == 32


def test_app_error_and_unhandled_error(app: FastAPI) -> None:
    router = APIRouter()

    @router.get("/_test/missing")
    def missing() -> None:
        raise NotFound()

    @router.get("/_test/boom")
    def boom() -> None:
        raise RuntimeError("secret detail that must not leak")

    app.include_router(router)
    with TestClient(app) as client:
        r = client.get("/_test/missing")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"

        r = client.get("/_test/boom")
        assert r.status_code == 500
        assert r.json()["error"] == {
            "code": "internal_error",
            "message": "Something went wrong.",
            "request_id": r.headers["X-Request-ID"],
        }
        assert "secret" not in r.text
