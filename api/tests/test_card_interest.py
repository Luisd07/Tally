from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Account, User
from app.models.enums import AccountKind
from app.schemas.accounts import CardInterestOut
from app.services.accounts import get_interest_summary, list_cards
from tests.factories import make_snapshot, make_transaction

TODAY = date(2026, 10, 15)


def _card(db: Session, user: User, **kwargs: object) -> Account:
    defaults: dict[str, object] = {
        "user_id": user.id,
        "name": "Freedom",
        "kind": AccountKind.credit_card,
    }
    defaults.update(kwargs)
    card = Account(**defaults)
    db.add(card)
    db.flush()
    return card


def _interest(db: Session, user: User, card: Account, posted: date, amount: str) -> None:
    db.add(
        make_transaction(
            user,
            card,
            posted_date=posted,
            amount=Decimal(amount),
            description_raw="INTEREST CHARGED ON PURCHASES",
        )
    )


def _interest_of(db: Session, user: User, card: Account) -> CardInterestOut:
    db.flush()
    cards = list_cards(db, user.id, include_inactive=False, today=TODAY)
    interest = next(c.interest for c in cards if c.account.id == card.id)
    assert interest is not None
    return interest


def test_groups_interest_by_month_over_the_last_twelve_months(
    db_session: Session, user: User
) -> None:
    card = _card(db_session, user)
    _interest(db_session, user, card, date(2026, 10, 3), "-10.00")
    _interest(db_session, user, card, date(2026, 9, 30), "-20.00")
    _interest(db_session, user, card, date(2026, 9, 1), "-5.50")
    _interest(db_session, user, card, date(2025, 11, 2), "-7.00")
    _interest(db_session, user, card, date(2025, 10, 31), "-99.00")  # before the window
    db_session.add(make_transaction(user, card, posted_date=date(2026, 9, 5), amount="-40.00"))

    interest = _interest_of(db_session, user, card)

    months = {m.month: m.amount for m in interest.months}
    assert list(months)[0] == "2025-11"
    assert list(months)[-1] == "2026-10"
    assert len(months) == 12
    assert months["2026-10"] == Decimal("10.00")
    assert months["2026-09"] == Decimal("25.50")
    assert months["2025-11"] == Decimal("7.00")
    assert months["2026-08"] == Decimal("0.00")


def test_year_to_date_counts_only_this_year(db_session: Session, user: User) -> None:
    card = _card(db_session, user)
    _interest(db_session, user, card, date(2026, 1, 2), "-3.00")
    _interest(db_session, user, card, date(2026, 10, 3), "-4.00")
    _interest(db_session, user, card, date(2025, 12, 31), "-50.00")

    assert _interest_of(db_session, user, card).year_to_date == Decimal("7.00")


def test_pending_and_credits_are_not_interest(db_session: Session, user: User) -> None:
    card = _card(db_session, user)
    db_session.add(
        make_transaction(
            user,
            card,
            posted_date=date(2026, 10, 3),
            amount=Decimal("-9.00"),
            description_raw="INTEREST CHARGE",
            pending=True,
        )
    )
    db_session.add(
        make_transaction(
            user,
            card,
            posted_date=date(2026, 10, 4),
            amount=Decimal("9.00"),
            description_raw="INTEREST ADJUSTMENT",
        )
    )

    assert _interest_of(db_session, user, card).year_to_date == Decimal("0.00")


def test_estimate_is_owed_times_apr_over_twelve(db_session: Session, user: User) -> None:
    card = _card(db_session, user, apr=Decimal("24.00"))
    db_session.add(make_snapshot(user, card, date=date(2026, 10, 10), balance=Decimal("-1500.00")))

    assert _interest_of(db_session, user, card).estimate == Decimal("30.00")


def test_estimate_rounds_to_cents(db_session: Session, user: User) -> None:
    card = _card(db_session, user, apr=Decimal("24.99"))
    db_session.add(make_snapshot(user, card, date=date(2026, 10, 10), balance=Decimal("-1180.25")))

    assert _interest_of(db_session, user, card).estimate == Decimal("24.58")


def test_estimate_is_null_without_apr_or_balance(db_session: Session, user: User) -> None:
    no_apr = _card(db_session, user, name="No APR")
    db_session.add(make_snapshot(user, no_apr, date=date(2026, 10, 10), balance=Decimal("-100.00")))
    no_balance = _card(db_session, user, name="No balance", apr=Decimal("20.00"))

    assert _interest_of(db_session, user, no_apr).estimate is None
    assert _interest_of(db_session, user, no_balance).estimate is None


def test_estimate_is_zero_during_an_active_promo(db_session: Session, user: User) -> None:
    card = _card(db_session, user, apr=Decimal("24.00"), promo_apr_end=date(2026, 12, 1))
    db_session.add(make_snapshot(user, card, date=date(2026, 10, 10), balance=Decimal("-1500.00")))

    assert _interest_of(db_session, user, card).estimate == Decimal("0.00")


def test_estimate_resumes_once_the_promo_has_ended(db_session: Session, user: User) -> None:
    card = _card(db_session, user, apr=Decimal("24.00"), promo_apr_end=date(2026, 10, 15))
    db_session.add(make_snapshot(user, card, date=date(2026, 10, 10), balance=Decimal("-1500.00")))

    assert _interest_of(db_session, user, card).estimate == Decimal("30.00")


def test_credit_balance_estimates_zero(db_session: Session, user: User) -> None:
    card = _card(db_session, user, apr=Decimal("24.00"))
    db_session.add(make_snapshot(user, card, date=date(2026, 10, 10), balance=Decimal("50.00")))

    assert _interest_of(db_session, user, card).estimate == Decimal("0.00")


def _mismatch_for(db: Session, user: User, actual: str) -> bool:
    """A card that owed $1500 at 24% APR going into September (expects $30.00)."""
    card = _card(db, user, apr=Decimal("24.00"))
    db.add(make_snapshot(user, card, date=date(2026, 8, 31), balance=Decimal("-1500.00")))
    _interest(db, user, card, date(2026, 9, 20), actual)
    return _interest_of(db, user, card).mismatch


def test_mismatch_when_actual_is_far_from_the_estimate(db_session: Session, user: User) -> None:
    assert _mismatch_for(db_session, user, "-45.00") is True
    assert _mismatch_for(db_session, user, "-10.00") is True


def test_no_mismatch_within_the_ratio_or_the_dollar_floor(db_session: Session, user: User) -> None:
    assert _mismatch_for(db_session, user, "-37.00") is False  # 23% off, $7
    assert _mismatch_for(db_session, user, "-35.00") is False  # exactly $5 off, 17%


def test_no_mismatch_when_over_the_ratio_but_under_five_dollars(
    db_session: Session, user: User
) -> None:
    card = _card(db_session, user, apr=Decimal("12.00"))  # expects $4.00
    db_session.add(make_snapshot(user, card, date=date(2026, 8, 31), balance=Decimal("-400.00")))
    _interest(db_session, user, card, date(2026, 9, 20), "-8.00")  # 100% off, $4

    assert _interest_of(db_session, user, card).mismatch is False


def test_no_mismatch_without_apr_or_prior_balance(db_session: Session, user: User) -> None:
    no_apr = _card(db_session, user, name="No APR")
    db_session.add(make_snapshot(user, no_apr, date=date(2026, 8, 31), balance=Decimal("-1500.00")))
    _interest(db_session, user, no_apr, date(2026, 9, 20), "-99.00")
    no_balance = _card(db_session, user, name="No balance", apr=Decimal("24.00"))
    _interest(db_session, user, no_balance, date(2026, 9, 20), "-99.00")

    assert _interest_of(db_session, user, no_apr).mismatch is False
    assert _interest_of(db_session, user, no_balance).mismatch is False


def test_interest_during_a_promo_month_is_a_mismatch(db_session: Session, user: User) -> None:
    card = _card(db_session, user, apr=Decimal("24.00"), promo_apr_end=date(2026, 12, 1))
    db_session.add(make_snapshot(user, card, date=date(2026, 8, 31), balance=Decimal("-1500.00")))
    _interest(db_session, user, card, date(2026, 9, 20), "-30.00")

    assert _interest_of(db_session, user, card).mismatch is True


def test_summary_totals_cards_and_is_scoped_to_the_user(
    db_session: Session, user: User, other_user: User
) -> None:
    one = _card(db_session, user, name="One")
    two = _card(db_session, user, name="Two")
    theirs = _card(db_session, other_user, name="Theirs")
    _interest(db_session, user, one, date(2026, 10, 3), "-4.00")
    _interest(db_session, user, two, date(2026, 3, 3), "-6.50")
    _interest(db_session, other_user, theirs, date(2026, 10, 3), "-100.00")
    db_session.flush()

    summary = get_interest_summary(db_session, user.id, today=TODAY)

    assert summary.year_to_date == Decimal("10.50")
    assert set(summary.cards) == {one.id, two.id}


def test_interest_routes_return_the_new_fields(
    db_client: TestClient, db_session: Session, user: User, auth_headers: dict[str, str]
) -> None:
    card = _card(db_session, user, apr=Decimal("24.00"))
    db_session.add(
        make_snapshot(
            user,
            card,
            date=datetime.now(UTC).date() - timedelta(days=1),
            balance=Decimal("-1500.00"),
        )
    )
    db_session.flush()

    cards = db_client.get("/api/accounts/cards", headers=auth_headers).json()
    summary = db_client.get("/api/accounts/interest", headers=auth_headers)

    assert len(cards[0]["interest"]["months"]) == 12
    assert summary.status_code == 200
    assert str(card.id) in summary.json()["cards"]
    assert "year_to_date" in summary.json()


def _fee(db: Session, user: User, card: Account, posted: date, amount: str, text: str) -> None:
    db.add(
        make_transaction(
            user, card, posted_date=posted, amount=Decimal(amount), description_raw=text
        )
    )


def test_fees_are_split_from_interest_and_kept_out_of_the_apr_check(
    db_session: Session, user: User
) -> None:
    card = _card(db_session, user, apr=Decimal("24.00"))
    db_session.add(make_snapshot(user, card, date=date(2026, 8, 31), balance=Decimal("-1500.00")))
    _interest(db_session, user, card, date(2026, 9, 20), "-30.00")
    _fee(db_session, user, card, date(2026, 9, 21), "-95.00", "ANNUAL MEMBERSHIP FEE")

    interest = _interest_of(db_session, user, card)

    assert interest.mismatch is False
    sept = next(m for m in interest.months if m.month == "2026-09")
    assert (sept.amount, sept.interest, sept.fees) == (
        Decimal("125.00"),
        Decimal("30.00"),
        Decimal("95.00"),
    )
    assert interest.year_to_date == Decimal("125.00")
    assert interest.year_to_date_interest == Decimal("30.00")
    assert interest.year_to_date_fees == Decimal("95.00")


def test_a_reversal_cancels_its_fee_and_never_goes_below_zero(
    db_session: Session, user: User
) -> None:
    card = _card(db_session, user)
    _fee(db_session, user, card, date(2026, 9, 5), "-30.00", "LATE FEE")
    _fee(db_session, user, card, date(2026, 9, 12), "30.00", "LATE FEE REVERSAL")
    _fee(db_session, user, card, date(2026, 8, 5), "20.00", "FEE REFUND")

    interest = _interest_of(db_session, user, card)

    months = {m.month: m.amount for m in interest.months}
    assert months["2026-09"] == Decimal("0.00")
    assert months["2026-08"] == Decimal("0.00")
    assert interest.year_to_date == Decimal("0.00")


def test_stale_balance_gives_no_estimate_or_mismatch(db_session: Session, user: User) -> None:
    card = _card(db_session, user, apr=Decimal("24.00"))
    db_session.add(make_snapshot(user, card, date=date(2026, 6, 30), balance=Decimal("-1500.00")))
    _interest(db_session, user, card, date(2026, 9, 20), "-99.00")

    interest = _interest_of(db_session, user, card)

    assert interest.estimate is None
    assert interest.mismatch is False


def test_prior_balance_just_inside_the_window_still_counts(db_session: Session, user: User) -> None:
    card = _card(db_session, user, apr=Decimal("24.00"))
    db_session.add(make_snapshot(user, card, date=date(2026, 7, 18), balance=Decimal("-1500.00")))
    db_session.add(make_snapshot(user, card, date=date(2026, 9, 1), balance=Decimal("-1500.00")))
    _interest(db_session, user, card, date(2026, 9, 20), "-99.00")

    assert _interest_of(db_session, user, card).mismatch is True


def test_today_defaults_to_the_app_timezone(
    db_session: Session, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import accounts as service

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            return datetime(2027, 1, 1, 4, 0, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(service.dt, "datetime", _FrozenDatetime)
    card = _card(db_session, user)
    _interest(db_session, user, card, date(2026, 12, 20), "-8.00")
    _interest(db_session, user, card, date(2026, 12, 31), "-2.00")
    db_session.flush()

    interest = next(
        c.interest for c in list_cards(db_session, user.id, include_inactive=False) if c.interest
    )

    assert interest.months[-1].month == "2026-12"
    assert interest.year_to_date == Decimal("10.00")
