"""Run the AI Inbox suggestions (#169) now instead of waiting for the night.

    uv run python -m scripts.ai_suggestions submit    # send the Inbox rows to Claude
    uv run python -m scripts.ai_suggestions collect   # store the answers of ended batches

On the VM: `docker compose exec worker python -m scripts.ai_suggestions submit`. A batch
usually ends within the hour; the worker also collects every 10 minutes.
"""

import sys
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import make_engine
from app.models import User
from app.services import ai_suggestions


def main(argv: list[str]) -> int:
    if argv not in (["submit"], ["collect"]):
        print(__doc__)
        return 2
    settings = get_settings()
    engine = make_engine(settings)
    try:
        with Session(engine) as db:
            now = datetime.now(UTC)
            if argv == ["submit"]:
                owner = db.scalars(select(User).order_by(User.created_at).limit(1)).first()
                if owner is None:
                    print("No owner user yet.")
                    return 1
                batch = ai_suggestions.submit(db, settings, owner.id, now)
                db.commit()
                print(f"Submitted batch {batch.anthropic_id}." if batch else "Nothing submitted.")
            else:
                ended = ai_suggestions.collect(db, settings, now)
                db.commit()
                print(f"Collected {ended} ended batch(es).")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
