from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine

from app.models import Base


def test_migrations_match_models(engine: Engine) -> None:
    """Fails when a model changed without a migration (run `alembic revision --autogenerate`)."""
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []
