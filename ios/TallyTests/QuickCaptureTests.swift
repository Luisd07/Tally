import Foundation
import Testing
@testable import Tally

/// AI quick capture (#171): a draft fills the Add form for review.
@MainActor
struct QuickCaptureTests {
    let suiteName = "test.quickcapture.\(UUID().uuidString)"
    var preferences: CapturePreferences { CapturePreferences(defaults: UserDefaults(suiteName: suiteName)!) }

    func category(_ id: UUID, kind: String) throws -> Tally.Category {
        let json = """
        {"id":"\(id.uuidString.lowercased())","name":"Dining","kind":"\(kind)","tax_line":null,"deductible_pct":100,
         "monthly_budget":null,"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}
        """
        return try JSONDecoder.api.decode(Tally.Category.self, from: Data(json.utf8))
    }

    func draft(_ json: String) throws -> ExpenseDraft {
        try JSONDecoder.api.decode(ExpenseDraft.self, from: Data(json.utf8))
    }

    @Test func draftFillsTheForm() throws {
        let dining = UUID()
        let model = AddModel(preferences: preferences)
        model.categories = [try category(dining, kind: "expense")]

        model.apply(try draft("""
        {"direction":"expense","amount":"18.40","merchant":"Chipotle","description":"Client lunch",
         "date":"2026-10-22","category_id":"\(dining.uuidString.lowercased())","bucket":"business"}
        """))

        #expect(model.direction == .expense)
        #expect(model.amount == Money(Decimal(string: "18.40")!))
        #expect(model.merchant == "Chipotle")
        #expect(model.description == "Client lunch")
        #expect(model.date == CalendarDate(year: 2026, month: 10, day: 22))
        #expect(model.categoryId == dining)
        #expect(model.bucket == .business)
    }

    @Test func emptyFieldsKeepWhatsThereAndAMismatchedCategoryIsLeftOut() throws {
        let dining = UUID()
        let model = AddModel(preferences: preferences)
        model.categories = [try category(dining, kind: "expense")]
        model.merchant = "Typed"
        model.press(5)

        model.apply(try draft("""
        {"direction":"income","amount":null,"merchant":null,"description":null,"date":"2026-10-20",
         "category_id":"\(dining.uuidString.lowercased())","bucket":null}
        """))

        #expect(model.direction == .moneyIn)
        #expect(model.amount == Money(5))
        #expect(model.merchant == "Typed")
        #expect(model.categoryId == nil)
    }
}

// MARK: - Network (shares StubURLProtocol, so these run in the serialized suite)

extension APIClientTests {
    @Test func readingANoteSendsItAndFillsTheForm() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let body = #"{"direction":"expense","amount":"12.00","merchant":"Publix","description":null,"date":"2026-10-23","category_id":null,"bucket":"personal"}"#
        let requests = reply(200, body: Data(body.utf8))
        let model = AddModel(preferences: CapturePreferences(defaults: UserDefaults(suiteName: "test.note.\(UUID())")!))
        model.note = "  groceries publix 12  "

        await model.readNote(api: makeClient(session))

        #expect(model.amount == Money(12))
        #expect(model.merchant == "Publix")
        #expect(model.note.isEmpty)
        #expect(model.noteError == nil)
        var iterator = requests.makeAsyncIterator()
        let request = try #require(await iterator.next())
        #expect(request.url?.path() == "/api/ai/parse-expense")
        let sent = try #require(bodyData(request))
        #expect(String(decoding: sent, as: UTF8.self) == #"{"text":"groceries publix 12"}"#)
    }

    @Test func aNoteTheServerCantReadKeepsTheForm() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let envelope = #"{"error":{"code":"service_unavailable","message":"AI assist is off.","request_id":"r1"}}"#
        _ = reply(503, body: Data(envelope.utf8))
        let model = AddModel(preferences: CapturePreferences(defaults: UserDefaults(suiteName: "test.note.\(UUID())")!))
        model.note = "lunch 12"
        model.merchant = "Kept"

        await model.readNote(api: makeClient(session))

        #expect(model.noteError == "AI assist is off.")
        #expect(model.note == "lunch 12")
        #expect(model.merchant == "Kept")
    }
}
