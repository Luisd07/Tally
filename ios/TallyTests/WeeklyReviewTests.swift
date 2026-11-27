import Foundation
import Testing
@testable import Tally

// MARK: - Network (shares StubURLProtocol, so these run in the serialized suite)

extension APIClientTests {
    @Test func weeklyReviewDecodesItsFindings() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let id = UUID()
        let body = """
        {"week_start":"2026-10-19","week_end":"2026-10-25","summary":"You spent $240.","findings":[
          {"kind":"double_charge","note":"Charged twice.","transaction":{"id":"\(id.uuidString.lowercased())","posted_date":"2026-10-20","description_raw":"CHIPOTLE 2291","merchant":"Chipotle","amount":"-18.40"}},
          {"kind":"something_new","note":"Gone now.","transaction":null}]}
        """
        let requests = reply(200, body: Data(body.utf8))

        let review = try #require(try await makeClient(session).weeklyReview())

        #expect(review.weekStart == CalendarDate(year: 2026, month: 10, day: 19))
        #expect(review.summary == "You spent $240.")
        #expect(review.findings.count == 2)
        #expect(review.findings[0].kind == .doubleCharge)
        #expect(review.findings[0].transaction?.id == id)
        #expect(review.findings[0].transaction?.displayName == "Chipotle")
        #expect(review.findings[0].transaction?.amount == Money(Decimal(string: "-18.40")!))
        #expect(review.findings[1].kind == .other("something_new"))
        #expect(review.findings[1].transaction == nil)
        var iterator = requests.makeAsyncIterator()
        #expect(try #require(await iterator.next()).url?.path() == "/api/ai/weekly-review")
    }

    @Test func noWeeklyReviewYetIsNil() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        _ = reply(200, body: Data("null".utf8))
        #expect(try await makeClient(session).weeklyReview() == nil)
    }
}
