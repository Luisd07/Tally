"""Write last week's AI review (#173) now instead of waiting for Monday morning.

    uv run python -m scripts.weekly_review

On the VM: `docker compose exec worker python -m scripts.weekly_review`. Does nothing when
last week already has a review.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import make_engine
from app.models import User
from app.services import weekly_review


def main() -> None:
    settings = get_settings()
    engine = make_engine(settings)
    try:
        with Session(engine) as db:
            owner = db.scalars(select(User).order_by(User.created_at).limit(1)).first()
            if owner is None:
                print("No owner user yet.")
                return
            review = weekly_review.run_review(db, settings, owner.id, datetime.now(UTC))
            db.commit()
            print(f"Wrote the review for {review.week_start}." if review else "Nothing written.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
