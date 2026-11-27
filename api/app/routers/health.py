from fastapi import APIRouter

from app.schemas.health import Health

router = APIRouter()


@router.get("/healthz")
def healthz() -> Health:
    return Health(status="ok")
