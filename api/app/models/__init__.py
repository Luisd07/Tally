"""SQLAlchemy tables. Import every model here so Alembic autogenerate sees it."""

from app.models.account import Account
from app.models.ai_action import AIAction
from app.models.ai_suggestion import AIBatch, AISuggestion
from app.models.ai_usage import AIUsage
from app.models.app_attest_key import AppAttestChallenge, AppAttestKey
from app.models.auth_event import AuthEvent
from app.models.balance_snapshot import BalanceSnapshot
from app.models.base import Base
from app.models.category import Category
from app.models.chat import ChatConversation
from app.models.device import Device
from app.models.enrollment_code import EnrollmentCode
from app.models.event import Event
from app.models.import_mapping import ImportMapping
from app.models.income_entry import IncomeEntry
from app.models.insight import Insight
from app.models.mileage import MileageRate, MileageTrip, OdometerReading, SavedTrip
from app.models.passkey import Passkey
from app.models.receipt import Receipt
from app.models.receipt_reading import ReceiptReading
from app.models.refresh_token import RefreshToken
from app.models.reimbursement import Reimbursement
from app.models.rule import Rule
from app.models.simplefin_connection import SimplefinConnection
from app.models.subscription import Subscription
from app.models.sync_run import SyncRun
from app.models.tax_review import TaxReview, TaxReviewItem
from app.models.transaction import Transaction
from app.models.user import User
from app.models.webauthn_challenge import WebAuthnChallenge
from app.models.weekly_review import ReviewFinding, WeeklyReview

__all__ = [
    "AIAction",
    "AIBatch",
    "AISuggestion",
    "AIUsage",
    "Account",
    "AppAttestChallenge",
    "AppAttestKey",
    "AuthEvent",
    "BalanceSnapshot",
    "Base",
    "Category",
    "ChatConversation",
    "Device",
    "EnrollmentCode",
    "Event",
    "ImportMapping",
    "IncomeEntry",
    "Insight",
    "MileageRate",
    "MileageTrip",
    "OdometerReading",
    "Passkey",
    "Receipt",
    "ReceiptReading",
    "RefreshToken",
    "ReviewFinding",
    "Reimbursement",
    "Rule",
    "SavedTrip",
    "SimplefinConnection",
    "Subscription",
    "SyncRun",
    "TaxReview",
    "TaxReviewItem",
    "Transaction",
    "User",
    "WebAuthnChallenge",
    "WeeklyReview",
]
