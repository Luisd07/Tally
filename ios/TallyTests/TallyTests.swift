import Foundation
import Testing
@testable import Tally

@MainActor
struct TallyTests {
    private final class BundleToken {}

    private func fixture(_ name: String) throws -> Data {
        let url = try #require(Bundle(for: BundleToken.self).url(forResource: name, withExtension: "json"))
        return try Data(contentsOf: url)
    }

    @Test func decodesRealHomeSample() throws {
        let home = try JSONDecoder.api.decode(Home.self, from: fixture("home-2026-10"))
        #expect(home.month == "2026-10")
        #expect(home.spendingByBucket.buckets.first?.spent == Money(Decimal(string: "156.60")!))
        #expect(home.spendingByBucket.total == Money(Decimal(string: "376.60")!))
        #expect(home.billsDue.isEmpty)
        #expect(home.lastUpdated != nil)
        #expect(home.staleBanks == nil)
    }

    @Test func decodesStaleBanks() throws {
        let json = #"{"month":"2026-10","spending_by_bucket":{"buckets":[],"total":"0.00"},"owed_to_you":"0.00","income":{"expected":"0.00","received":"0.00"},"bills_due":[],"last_updated":null,"stale_banks":[{"institution":"Chase Bank","last_refreshed":"2026-10-24"}]}"#
        let home = try JSONDecoder.api.decode(Home.self, from: Data(json.utf8))
        #expect(home.staleBanks == [StaleBank(institution: "Chase Bank", lastRefreshed: CalendarDate(year: 2026, month: 10, day: 24))])
    }

    @Test func decodesHomeWithBills() throws {
        let home = try JSONDecoder.api.decode(Home.self, from: fixture("home-with-bills"))
        #expect(home.spendingByBucket.buckets[0].spent == Money(Decimal(string: "-85.00")!))
        #expect(home.spendingByBucket.buckets[1].bucket == .other("charity"))
        #expect(home.billsDue.count == 2)
        #expect(home.billsDue[0].kind == .subscription)
        #expect(home.billsDue[0].amount == Money(Decimal(string: "15.49")!))
        #expect(home.billsDue[1].kind == .card)
        #expect(home.billsDue[1].amount == nil)
        #expect(home.lastUpdated != nil)
    }

    @Test func decodesTimestampsWithAndWithoutFraction() throws {
        struct Wrapper: Decodable { let at: Date }
        let fractional = try JSONDecoder.api.decode(Wrapper.self, from: Data(#"{"at":"2026-10-17T20:42:10.064902Z"}"#.utf8))
        let plain = try JSONDecoder.api.decode(Wrapper.self, from: Data(#"{"at":"2026-10-17T20:42:10Z"}"#.utf8))
        #expect(abs(fractional.at.timeIntervalSince(plain.at) - 0.064902) < 0.001)
    }

    @Test func calendarDateDoesNotShiftDay() throws {
        struct Wrapper: Decodable { let day: CalendarDate }
        let wrapper = try JSONDecoder.api.decode(Wrapper.self, from: Data(#"{"day":"2026-10-08"}"#.utf8))
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "America/Los_Angeles")!
        calendar.locale = Locale(identifier: "en_US")
        #expect(wrapper.day == CalendarDate(year: 2026, month: 10, day: 8))
        #expect(wrapper.day.formatted(calendar: calendar) == "Oct 8")
    }

    @Test func mapsUnauthorizedEnvelope() throws {
        let error = APIError.from(status: 401, body: try fixture("error-401"), headerRequestId: nil)
        #expect(error.status == 401)
        #expect(error.code == "unauthorized")
        #expect(error.isUnauthorized)
        #expect(error.errorDescription == "Sign-in required.")
        #expect(error.requestId == "d91c93ea86f74a4f96343ac08cdf33ad")
    }

    @Test func mapsValidationEnvelope() throws {
        let error = APIError.from(status: 422, body: try fixture("error-422"), headerRequestId: nil)
        #expect(error.status == 422)
        #expect(error.code == "validation_error")
        #expect(!error.isUnauthorized)
        #expect(error.errorDescription == "month: month must be formatted YYYY-MM")
    }

    @Test func validationErrorNamesTheFieldItsLocEndsAt() {
        let body = #"{"error":{"code":"validation_error","message":"Invalid request.","request_id":"r1","details":[{"type":"greater_than","loc":["body","amount"],"msg":"Input should be greater than 0"},{"type":"missing","loc":["body","date"],"msg":"Field required"}]}}"#
        let error = APIError.from(status: 422, body: Data(body.utf8), headerRequestId: nil)
        #expect(error.errorDescription == "amount: Input should be greater than 0")
    }

    /// `UnprocessableEntity.value_error` in api/app/errors.py: loc is just `["body"]`, so no field is named.
    @Test func validationErrorAtTheBodyShowsOnlyTheReason() {
        let body = #"{"error":{"code":"validation_error","message":"Invalid request.","request_id":"r1","details":[{"type":"value_error","loc":["body"],"msg":"Value error, save a column mapping for this account first"}]}}"#
        let error = APIError.from(status: 422, body: Data(body.utf8), headerRequestId: nil)
        #expect(error.errorDescription == "save a column mapping for this account first")
    }

    @Test func validationErrorEndingAtAListIndexShowsOnlyTheReason() {
        let body = #"{"error":{"code":"validation_error","message":"Invalid request.","details":[{"loc":["body","splits",0],"msg":"Value error, splits must add up"}]}}"#
        let error = APIError.from(status: 422, body: Data(body.utf8), headerRequestId: nil)
        #expect(error.errorDescription == "splits must add up")
    }

    @Test func validationErrorShowsServerReason() {
        let body = #"{"error":{"code":"validation_error","message":"Invalid request.","details":[{"msg":"Value error, save a column mapping for this account first"}]}}"#
        let error = APIError.from(status: 422, body: Data(body.utf8), headerRequestId: nil)
        #expect(error.errorDescription == "save a column mapping for this account first")
    }

    @Test func validationErrorWithoutDetailsKeepsMessage() {
        let body = #"{"error":{"code":"validation_error","message":"Invalid request."}}"#
        let error = APIError.from(status: 422, body: Data(body.utf8), headerRequestId: nil)
        #expect(error.errorDescription == "Invalid request.")
    }

    @Test func nonValidationErrorKeepsMessage() {
        let body = #"{"error":{"code":"not_found","message":"Not found.","details":[{"msg":"Value error, nope"}]}}"#
        let error = APIError.from(status: 404, body: Data(body.utf8), headerRequestId: nil)
        #expect(error.errorDescription == "Not found.")
    }

    @Test func fallsBackWhenBodyIsNotEnvelope() {
        let error = APIError.from(status: 502, body: Data("<html>".utf8), headerRequestId: "abc")
        #expect(error.requestId == "abc")
        #expect(error.errorDescription == "Request failed (HTTP 502).")
    }
}

@MainActor
struct KeychainTests {
    @Test func roundTripsAndOverwrites() throws {
        let key = "test.keychain.roundtrip"
        try? Keychain.delete(key)
        try Keychain.save("one", for: key)
        #expect(try Keychain.read(key) == "one")
        try Keychain.save("two", for: key)
        #expect(try Keychain.read(key) == "two")
        try Keychain.delete(key)
        #expect(try Keychain.read(key) == nil)
    }
}

@MainActor
struct GettingStartedTests {
    private func home(setup: String) throws -> Home {
        let json = #"{"month":"2026-10","spending_by_bucket":{"buckets":[],"total":"0.00"},"owed_to_you":"0.00","income":{"expected":"0.00","received":"0.00"},"bills_due":[],"last_updated":null"# + setup + "}"
        return try JSONDecoder.api.decode(Home.self, from: Data(json.utf8))
    }

    private func progress(
        bank: Bool = false, inbox: Bool = false, categories: Bool = false, budgets: Bool = false, notifications: Bool = false
    ) -> SetupProgress {
        SetupProgress(
            bankConnected: bank, inboxReviewed: inbox, categoriesReady: categories, budgetsSet: budgets,
            notificationsOn: notifications
        )
    }

    @Test func decodesSetupWhenPresent() throws {
        let decoded = try home(setup: #","setup":{"bank_connected":true,"inbox_reviewed":false,"categories_ready":true,"budgets_set":false,"notifications_on":false}"#)
        #expect(decoded.setup == progress(bank: true, categories: true))
    }

    @Test func decodesWithoutSetupFromAnOlderServer() throws {
        #expect(try home(setup: "").setup == nil)
    }

    @Test func stepsShowAsDoneFromTheirFlags() {
        let setup = progress(bank: true, budgets: true)
        #expect(GettingStartedStep.allCases.map { $0.isDone(setup) } == [true, false, false, true, false])
    }

    @Test func sectionShowsWhileAnyStepIsOpen() {
        #expect(GettingStartedStep.isVisible(progress(), dismissed: false))
        #expect(GettingStartedStep.isVisible(progress(bank: true, inbox: true, categories: true, budgets: true), dismissed: false))
    }

    @Test func sectionHidesWhenAllDoneOrDismissed() {
        let all = progress(bank: true, inbox: true, categories: true, budgets: true, notifications: true)
        #expect(!GettingStartedStep.isVisible(all, dismissed: false))
        #expect(!GettingStartedStep.isVisible(progress(), dismissed: true))
    }

    @Test func signOutClearsTheDismissal() throws {
        let fixture = TestSession()
        defer { try? fixture.cleanUp() }
        let prefs = GettingStartedPreferences(defaults: fixture.defaults)
        prefs.dismissed = true
        #expect(prefs.dismissed)
        fixture.make().signOut()
        #expect(!prefs.dismissed)
    }
}
