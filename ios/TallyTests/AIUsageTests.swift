import Foundation
import Testing
@testable import Tally

// MARK: - Network (shares StubURLProtocol, so these run in the serialized suite)

extension APIClientTests {
    @Test func aiUsageDecodesTheMonthsSpend() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let body = #"{"month":"2026-10","spent_usd":"0.12","budget_usd":"90.00","enabled":true}"#
        let requests = reply(200, body: Data(body.utf8))

        let usage = try await makeClient(session).aiUsage()

        #expect(usage.month == "2026-10")
        #expect(usage.spentUsd == Money(Decimal(string: "0.12")!))
        #expect(usage.budgetUsd == Money(90))
        #expect(usage.enabled)
        var iterator = requests.makeAsyncIterator()
        let request = try #require(await iterator.next())
        #expect(request.httpMethod == "GET")
        #expect(request.url?.path() == "/api/ai/usage")
    }
}
