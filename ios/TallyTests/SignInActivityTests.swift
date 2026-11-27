import Foundation
import os
import Testing
@testable import Tally

extension APIClientTests {
    private static let eventsPage = #"""
    {"items":[
      {"id":"00000000-0000-0000-0000-000000000001","kind":"refresh","ip":"203.0.113.5","user_agent":"Tally/1","created_at":"2026-10-24T03:00:00Z"},
      {"id":"00000000-0000-0000-0000-000000000002","kind":"sign_in_failed","ip":"198.51.100.7","user_agent":null,"created_at":"2026-10-24T02:00:00Z"},
      {"id":"00000000-0000-0000-0000-000000000003","kind":"sign_in","ip":"203.0.113.5","user_agent":"Tally/1","created_at":"2026-10-24T01:16:55Z"},
      {"id":"00000000-0000-0000-0000-000000000004","kind":"something_new","ip":null,"user_agent":null,"created_at":"2026-10-24T01:00:00Z"}
    ],"next_cursor":"abc"}
    """#

    @Test func signInActivityHidesRenewalsAndFlagsFailures() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t", serverURL: server)
        let api = makeClient(session)
        let requests = reply(200, body: Data(Self.eventsPage.utf8))
        let model = SignInActivityModel()

        await model.load(api: api)
        #expect(model.events.count == 4)
        #expect(model.visible.map(\.kind) == [.signInFailed, .signIn, .other("something_new")])
        #expect(model.failureCount == 1)
        #expect(model.hasMore)
        model.showsRenewals = true
        #expect(model.visible.count == 4)

        var iterator = requests.makeAsyncIterator()
        let request = try #require(await iterator.next())
        #expect(request.url?.path() == "/api/auth/events")
        #expect(request.url?.query()?.contains("limit=100") == true)
    }

    @Test func loadMoreSendsTheCursorAndSkipsDuplicates() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t", serverURL: server)
        let api = makeClient(session)
        let first = Data(Self.eventsPage.utf8)
        let second = Data(#"{"items":[{"id":"00000000-0000-0000-0000-000000000004","kind":"enroll","ip":null,"user_agent":null,"created_at":"2026-10-24T01:00:00Z"},{"id":"00000000-0000-0000-0000-000000000005","kind":"enroll","ip":null,"user_agent":null,"created_at":"2026-10-23T01:00:00Z"}],"next_cursor":null}"#.utf8)
        let cursors = OSAllocatedUnfairLock(initialState: [String?]())
        StubURLProtocol.respond { request in
            let cursor = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?
                .queryItems?.first { $0.name == "cursor" }?.value
            cursors.withLock { $0.append(cursor) }
            return (StubURLProtocol.response(to: request, status: 200), cursor == nil ? first : second)
        }
        let model = SignInActivityModel()

        await model.load(api: api)
        await model.loadMore(api: api)
        #expect(cursors.withLock { $0 } == [nil, "abc"])
        #expect(model.events.count == 5)
        #expect(!model.hasMore)
    }
}
