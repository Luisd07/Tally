import Foundation
import os
import Testing
import UIKit
@testable import Tally

/// A `Session` on its own Keychain item and UserDefaults suite, so the app's real sign-in is untouched.
@MainActor
struct TestSession {
    let tokenKey = "test.session.\(UUID().uuidString)"
    let suiteName = "test.session.\(UUID().uuidString)"
    var defaults: UserDefaults { UserDefaults(suiteName: suiteName)! }

    func make(tokens: any TokenService = UnusedTokenService()) -> Session {
        Session(tokenKey: tokenKey, defaults: defaults, tokens: tokens)
    }

    func cleanUp() throws {
        try Keychain.delete(tokenKey)
        try Keychain.delete("\(tokenKey).refresh")
        defaults.removePersistentDomain(forName: suiteName)
    }
}

/// Every test here shares `StubURLProtocol`'s handler, so they run one at a time. None touches the network.
@MainActor
@Suite(.serialized)
struct APIClientTests {
    let server = URL(string: "http://127.0.0.1:8000")!
    let fixture = TestSession()

    private final class BundleToken {}

    /// The fixture `name.ext` from `Fixtures/`.
    func bundleFixture(_ name: String, ext: String) throws -> Data {
        let url = try #require(Bundle(for: BundleToken.self).url(forResource: name, withExtension: ext))
        return try Data(contentsOf: url)
    }

    /// The JSON fixture `name` from `Fixtures/`.
    func jsonFixture(_ name: String) throws -> Data {
        try bundleFixture(name, ext: "json")
    }

    func makeClient(_ session: Session) -> APIClient {
        APIClient(credentials: session, urlSession: StubURLProtocol.makeSession())
    }

    /// Replies with `status` to every request and streams each request it saw.
    func reply(_ status: Int, body: Data = Data(), headers: [String: String] = [:]) -> AsyncStream<URLRequest> {
        let (requests, seen) = AsyncStream.makeStream(of: URLRequest.self)
        StubURLProtocol.respond { request in
            seen.yield(request)
            return (StubURLProtocol.response(to: request, status: status, headers: headers), body)
        }
        return requests
    }

    /// The body a request carried; `URLProtocol` sees it as a stream, not `httpBody`.
    func bodyData(_ request: URLRequest) -> Data? {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 1024)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count <= 0 { break }
            data.append(buffer, count: count)
        }
        return data
    }

    private func inboxItemJSON(id: UUID, unmatched: Bool, reviewed: Bool = false) -> String {
        """
        {"id":"\(id.uuidString.lowercased())","account_id":"265cd02e-4a77-51ea-ad3a-4a9fbc282065","posted_date":"2026-10-08","transacted_at":null,"amount":"-85.00","description_raw":"HERTZ","merchant":null,"category_id":null,"bucket":"personal","source":"manual","external_id":null,"pending":false,"reviewed":\(reviewed),"notes":null,"event_id":null,"parent_id":null,"reimbursement_id":null,"is_split":false,"created_at":"2026-10-18T20:42:06.089964Z","updated_at":"2026-10-18T20:42:06.089964Z","needs_resplit":false,"unmatched_manual":\(unmatched),"suggested_category_id":null,"suggested_bucket":null}
        """
    }

    private func inboxPageJSON(ids: [UUID], cursor: String?, total: Int) -> String {
        let items = ids.map { inboxItemJSON(id: $0, unmatched: false) }.joined(separator: ",")
        let next = cursor.map { "\"\($0)\"" } ?? "null"
        return #"{"items":[\#(items)],"next_cursor":\#(next),"total":\#(total)}"#
    }

    @Test func sendsBearerHeaderOnlyWithToken() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        let api = makeClient(session)
        var requests = reply(204).makeAsyncIterator()

        let _: APIClient.Empty = try await api.get("/api/home")
        #expect(await requests.next()?.value(forHTTPHeaderField: "Authorization") == nil)

        try session.signIn(token: "abc", serverURL: server)
        let _: APIClient.Empty = try await api.get("/api/home")
        #expect(await requests.next()?.value(forHTTPHeaderField: "Authorization") == "Bearer abc")
    }

    @Test func defaultSessionKeepsNoURLCache() {
        let configuration = APIClient.defaultSession.configuration
        #expect(configuration.urlCache == nil)
        #expect(configuration.requestCachePolicy == .reloadIgnoringLocalCacheData)
    }

    @Test func encodesQueryItems() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        var requests = reply(204).makeAsyncIterator()
        let query = [URLQueryItem(name: "month", value: "2026-10"), URLQueryItem(name: "q", value: "a&b c")]

        let _: APIClient.Empty = try await api.get("/api/home", query: query)
        let url = try #require(await requests.next()?.url)
        #expect(url.path() == "/api/home")
        #expect(URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems == query)
    }

    @Test func homeEndpointRequestsMonthAndDecodes() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let body = Data(#"{"month":"2026-10","spending_by_bucket":{"buckets":[],"total":"0.00"},"owed_to_you":"0.00","income":{"expected":"0.00","received":"0.00"},"bills_due":[],"last_updated":null}"#.utf8)
        var requests = reply(200, body: body).makeAsyncIterator()

        let home = try await api.home(month: MonthRef(year: 2026, month: 10))
        let url = try #require(await requests.next()?.url)
        #expect(url.path() == "/api/home")
        #expect(URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems == [URLQueryItem(name: "month", value: "2026-10")])
        #expect(home.month == "2026-10")
    }

    @Test func eventsEndpointSendsTheYearAndDecodes() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let body = Data("""
        [{"id":"6f1c2d3e-0000-4000-8000-000000000001","name":"Conference","series":null,"start_date":"2026-03-02",
          "end_date":"2026-03-05","location":"Austin","created_at":"2026-01-01T00:00:00Z",
          "updated_at":"2026-01-01T00:00:00Z","costs_total":"10.00","pay_total":"0.00"}]
        """.utf8)
        var requests = reply(200, body: body).makeAsyncIterator()

        let events = try await api.events(year: 2026)
        let request = try #require(await requests.next())
        let url = try #require(request.url)
        #expect(request.httpMethod == "GET")
        #expect(url.path() == "/api/events")
        #expect(URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems == [URLQueryItem(name: "year", value: "2026")])
        #expect(events.map(\.name) == ["Conference"])
        #expect(events.first?.startDate == CalendarDate(year: 2026, month: 3, day: 2))
        #expect(events.first?.endDate == CalendarDate(year: 2026, month: 3, day: 5))

        var plain = reply(200, body: Data("[]".utf8)).makeAsyncIterator()
        _ = try await api.events()
        let unfiltered = try #require(await plain.next())
        #expect(unfiltered.url?.query == nil)
    }

    @Test func inboxEndpointSendsLimitAndCursor() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let body = Data(#"{"items":[],"next_cursor":null,"total":0}"#.utf8)
        var requests = reply(200, body: body).makeAsyncIterator()

        let page = try await api.inbox(limit: 2, cursor: "abc=")
        let request = try #require(await requests.next())
        let url = try #require(request.url)
        #expect(request.httpMethod == "GET")
        #expect(url.path() == "/api/inbox")
        #expect(URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems == [
            URLQueryItem(name: "limit", value: "2"),
            URLQueryItem(name: "cursor", value: "abc="),
        ])
        #expect(page.total == 0)
        #expect(page.nextCursor == nil)
    }

    @Test func updateTransactionPatchesJSONAndDecodes() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let inbox = try JSONDecoder.api.decode(InboxPage.self, from: jsonFixture("inbox-page"))
        let item = try #require(inbox.items.first)
        let row = try JSONSerialization.jsonObject(with: jsonFixture("inbox-page")) as? [String: Any]
        let first = try #require((row?["items"] as? [[String: Any]])?.first)
        var requests = reply(200, body: try JSONSerialization.data(withJSONObject: first)).makeAsyncIterator()

        let updated = try await api.updateTransaction(id: item.id, patch: TransactionPatch(reviewed: true))
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "PATCH")
        #expect(request.url?.path() == "/api/transactions/\(item.id.uuidString.lowercased())")
        #expect(request.value(forHTTPHeaderField: "Content-Type") == "application/json")
        #expect(updated.id == item.id)
    }

    @Test func updateTransactionBodyHasExactlyTheSetKeys() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        var requests = reply(200, body: Data(inboxItemJSON(id: UUID(), unmatched: false).utf8)).makeAsyncIterator()

        let patch = TransactionPatch(categoryId: .some(nil), bucket: .business, reviewed: true)
        _ = try await api.updateTransaction(id: UUID(), patch: patch)
        let body = try #require(await requests.next().flatMap(bodyData))
        let json = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
        #expect(Set(json.keys) == ["category_id", "bucket", "reviewed"])
        #expect(json["category_id"] is NSNull)
        #expect(json["bucket"] as? String == "business")
        #expect(json["reviewed"] as? Bool == true)
    }

    @Test func createTransactionPostsExactlyTheCreateKeys() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let capture = Capture(
            accountId: UUID(),
            postedDate: CalendarDate(year: 2026, month: 10, day: 8),
            amount: Money(Decimal(string: "-85")!),
            descriptionRaw: "HERTZ",
            categoryId: UUID(),
            transactedAt: Date(timeIntervalSince1970: 1_790_000_000),
            receiptId: UUID()
        )
        var requests = reply(201, body: Data(inboxItemJSON(id: capture.id, unmatched: false).utf8)).makeAsyncIterator()

        let created = try await api.createTransaction(capture)
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "POST")
        #expect(request.url?.path() == "/api/transactions")
        let body = try #require(bodyData(request))
        let json = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
        #expect(Set(json.keys) == ["id", "account_id", "posted_date", "amount", "description_raw", "category_id", "bucket", "transacted_at"])
        #expect(json["id"] as? String == capture.id.uuidString)
        #expect(json["amount"] as? String == "-85.00")
        #expect(json["posted_date"] as? String == "2026-10-08")
        #expect(json["bucket"] as? String == "personal")
        #expect(created.id == capture.id)
    }

    @Test func inboxLoadsTwoPagesWithCursor() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = (0..<3).map { _ in UUID() }
        let first = inboxPageJSON(ids: Array(ids[0..<2]), cursor: "c1", total: 3)
        let second = inboxPageJSON(ids: [ids[2]], cursor: nil, total: 3)
        let (requests, seen) = AsyncStream.makeStream(of: URLRequest.self)
        StubURLProtocol.respond { request in
            seen.yield(request)
            let cursor = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "cursor" }
            return (StubURLProtocol.response(to: request, status: 200), Data((cursor == nil ? first : second).utf8))
        }
        var iterator = requests.makeAsyncIterator()
        let model = InboxModel()

        await model.load(api: api)
        #expect(model.items.map(\.id) == Array(ids[0..<2]))
        #expect(model.hasMore)
        await model.loadMore(api: api)

        _ = await iterator.next()
        let secondRequest = try #require(await iterator.next())
        let query = URLComponents(url: try #require(secondRequest.url), resolvingAgainstBaseURL: false)?.queryItems
        #expect(query?.contains(URLQueryItem(name: "cursor", value: "c1")) == true)
        #expect(model.items.map(\.id) == ids)
        #expect(!model.hasMore)
        #expect(model.total == 3)
        await model.loadMore(api: api)
        #expect(model.items.count == 3)
    }

    @Test func confirmRemovesRowAndSendsReviewedOnly() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = (0..<2).map { _ in UUID() }
        let model = InboxModel()
        _ = reply(200, body: Data(inboxPageJSON(ids: ids, cursor: nil, total: 2).utf8))
        await model.load(api: api)

        var requests = reply(200, body: Data(inboxItemJSON(id: ids[0], unmatched: false, reviewed: true).utf8)).makeAsyncIterator()
        await model.confirm(model.items[0], api: api)

        let body = try #require(await requests.next().flatMap(bodyData))
        #expect(String(decoding: body, as: UTF8.self) == #"{"reviewed":true}"#)
        #expect(model.items.map(\.id) == [ids[1]])
        #expect(model.total == 1)
        #expect(model.editError == nil)
    }

    @Test func inboxRefreshInFlightDuringConfirmDoesNotBringTheRowBack() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = (0..<2).map { _ in UUID() }
        let model = InboxModel()
        _ = reply(200, body: Data(inboxPageJSON(ids: ids, cursor: nil, total: 2).utf8))
        await model.load(api: api)
        let confirmed = inboxItemJSON(id: ids[0], unmatched: false, reviewed: true)
        let held = holdLoads(
            page: Data(inboxPageJSON(ids: ids, cursor: nil, total: 2).utf8),
            reloaded: Data(inboxPageJSON(ids: [ids[1]], cursor: nil, total: 1).utf8),
            other: { _ in (200, confirmed) }
        )

        let slow = Task { await model.load(api: api) }
        for await _ in held.arrived { break }
        await model.confirm(model.items[0], api: api)
        held.release.yield()
        await slow.value

        #expect(model.items.map(\.id) == [ids[1]])
        #expect(model.total == 1)
        #expect(model.editError == nil)
    }

    @Test func failedConfirmRestoresRowAtSameIndex() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = (0..<3).map { _ in UUID() }
        let model = InboxModel()
        _ = reply(200, body: Data(inboxPageJSON(ids: ids, cursor: nil, total: 3).utf8))
        await model.load(api: api)

        let (received, receivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let envelope = #"{"error":{"code":"validation_error","message":"Bucket is required.","request_id":"r1"}}"#
        StubURLProtocol.respond { request in
            receivedSignal.yield()
            for await _ in release { break }
            return (StubURLProtocol.response(to: request, status: 422), Data(envelope.utf8))
        }
        let item = model.items[1]
        let inFlight = Task { await model.confirm(item, api: api) }
        for await _ in received { break }

        #expect(model.items.map(\.id) == [ids[0], ids[2]])
        #expect(model.total == 2)

        releaseSignal.yield()
        await inFlight.value
        #expect(model.items.map(\.id) == ids)
        #expect(model.total == 3)
        #expect(model.editError?.error.errorDescription == "Bucket is required.")
        #expect(model.editError?.rowName == "HERTZ")
    }

    @Test func confirmingUnmatchedManualKeepsRowAsReviewed() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let id = UUID()
        let model = InboxModel()
        let page = #"{"items":[\#(inboxItemJSON(id: id, unmatched: true))],"next_cursor":null,"total":1}"#
        _ = reply(200, body: Data(page.utf8))
        await model.load(api: api)

        _ = reply(200, body: Data(inboxItemJSON(id: id, unmatched: true, reviewed: true).utf8))
        await model.confirm(model.items[0], api: api)

        #expect(model.items.count == 1)
        #expect(model.items[0].transaction.reviewed)
        #expect(model.total == 1)
        #expect(model.editError == nil)
    }

    @Test func failedRefreshKeepsItemsAndSetsError() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = [UUID()]
        let model = InboxModel()
        _ = reply(200, body: Data(inboxPageJSON(ids: ids, cursor: nil, total: 1).utf8))
        await model.load(api: api)
        #expect(model.error == nil)

        _ = reply(500, body: Data(#"{"error":{"code":"internal","message":"Boom.","request_id":"r2"}}"#.utf8))
        await model.load(api: api)
        #expect(model.items.map(\.id) == ids)
        #expect(model.total == 1)
        #expect(model.error?.status == 500)
    }

    func uuidString(_ id: UUID) -> String { id.uuidString.lowercased() }

    /// Holds each PATCH until its id is released, then answers with that id's outcome. Every other request gets `others` with a 200.
    func holdPatches(
        _ outcomes: [UUID: (status: Int, body: String)],
        others: @escaping @Sendable (URLRequest) -> String
    ) -> (arrived: AsyncStream<UUID>, release: [UUID: AsyncStream<Void>.Continuation]) {
        let (arrived, arrivedSignal) = AsyncStream.makeStream(of: UUID.self)
        var gates: [UUID: AsyncStream<Void>] = [:]
        var releases: [UUID: AsyncStream<Void>.Continuation] = [:]
        for id in outcomes.keys {
            (gates[id], releases[id]) = AsyncStream.makeStream(of: Void.self)
        }
        let waiting = gates
        StubURLProtocol.respond { request in
            guard request.httpMethod == "PATCH", let id = UUID(uuidString: request.url!.lastPathComponent) else {
                return (StubURLProtocol.response(to: request, status: 200), Data(others(request).utf8))
            }
            arrivedSignal.yield(id)
            for await _ in waiting[id]! { break }
            let outcome = outcomes[id]!
            return (StubURLProtocol.response(to: request, status: outcome.status), Data(outcome.body.utf8))
        }
        return (arrived, releases)
    }

    func waitForPatches(_ count: Int, in arrived: AsyncStream<UUID>) async {
        var seen = 0
        for await _ in arrived {
            seen += 1
            if seen == count { break }
        }
    }

    let validationError = (status: 422, body: #"{"error":{"code":"validation_error","message":"Nope.","request_id":"r1"}}"#)

    @Test func reloadDuringFailedEditLeavesNoDuplicateRows() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = (0..<3).map { _ in UUID() }
        let page = inboxPageJSON(ids: ids, cursor: nil, total: 3)
        let model = InboxModel()
        _ = reply(200, body: Data(page.utf8))
        await model.load(api: api)

        let held = holdPatches([ids[1]: validationError], others: { _ in page })
        let item = model.items[1]
        let inFlight = Task { await model.confirm(item, api: api) }
        await waitForPatches(1, in: held.arrived)
        await model.load(api: api)
        #expect(model.items.map(\.id) == ids)

        held.release[ids[1]]?.yield()
        await inFlight.value
        #expect(model.items.map(\.id) == ids)
        #expect(model.total == 3)
        #expect(model.editError?.rowName == "HERTZ")
    }

    @Test func reloadDuringSucceededUnmatchedEditAppliesServerReplyToReloadedRow() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let id = UUID()
        let page = #"{"items":[\#(inboxItemJSON(id: id, unmatched: true))],"next_cursor":null,"total":1}"#
        let model = InboxModel()
        _ = reply(200, body: Data(page.utf8))
        await model.load(api: api)

        let saved = inboxItemJSON(id: id, unmatched: true, reviewed: true)
        let held = holdPatches([id: (status: 200, body: saved)], others: { _ in page })
        let item = model.items[0]
        let inFlight = Task { await model.confirm(item, api: api) }
        await waitForPatches(1, in: held.arrived)
        await model.load(api: api)

        held.release[id]?.yield()
        await inFlight.value
        #expect(model.items.count == 1)
        #expect(model.items[0].transaction.reviewed)
        #expect(model.total == 1)
        #expect(model.editError == nil)
    }

    @Test func unmatchedConfirmShowsTheServersFields() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let id = UUID()
        let model = InboxModel()
        let page = #"{"items":[\#(inboxItemJSON(id: id, unmatched: true))],"next_cursor":null,"total":1}"#
        _ = reply(200, body: Data(page.utf8))
        await model.load(api: api)

        let serverRow = inboxItemJSON(id: id, unmatched: true, reviewed: true)
            .replacingOccurrences(of: #""notes":null"#, with: #""notes":"Set on the server""#)
            .replacingOccurrences(of: #""bucket":"personal""#, with: #""bucket":"business""#)
        _ = reply(200, body: Data(serverRow.utf8))
        await model.confirm(model.items[0], api: api)

        let transaction = model.items[0].transaction
        #expect(transaction.notes == "Set on the server")
        #expect(transaction.bucket == .business)
        #expect(transaction.reviewed)
    }

    @Test func applySuggestionSendsOnlyTheSuggestedKeys() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let categoryId = UUID()
        let suggested = inboxItemJSON(id: UUID(), unmatched: false)
            .replacingOccurrences(of: #""suggested_category_id":null"#, with: #""suggested_category_id":"\#(uuidString(categoryId))""#)
        let bare = inboxItemJSON(id: UUID(), unmatched: false)
        let page = #"{"items":[\#(suggested),\#(bare)],"next_cursor":null,"total":2}"#
        let model = InboxModel()
        _ = reply(200, body: Data(page.utf8))
        await model.load(api: api)

        var requests = reply(200, body: Data(suggested.utf8)).makeAsyncIterator()
        await model.applySuggestion(model.items[0], api: api)
        let withCategory = try #require(await requests.next().flatMap(bodyData))
        let first = try #require(JSONSerialization.jsonObject(with: withCategory) as? [String: Any])
        #expect(Set(first.keys) == ["category_id", "reviewed"])
        #expect((first["category_id"] as? String)?.lowercased() == uuidString(categoryId))

        await model.applySuggestion(model.items[0], api: api)
        let reviewedOnly = try #require(await requests.next().flatMap(bodyData))
        #expect(String(decoding: reviewedOnly, as: UTF8.self) == #"{"reviewed":true}"#)
    }

    private func aiSuggestedJSON(id: UUID, categoryId: UUID, ruleWorthy: Bool) -> String {
        inboxItemJSON(id: id, unmatched: false).replacingOccurrences(
            of: #""suggested_category_id":null,"suggested_bucket":null"#,
            with: #""suggested_category_id":"\#(uuidString(categoryId))","suggested_bucket":"business","suggestion_source":"ai","suggested_merchant":"Hertz","rule_worthy":\#(ruleWorthy)"#
        )
    }

    @Test func aiSuggestionSetsTheMerchantAndOffersARuleWhenWorthy() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let categoryId = UUID()
        let worthy = aiSuggestedJSON(id: UUID(), categoryId: categoryId, ruleWorthy: true)
        let plain = aiSuggestedJSON(id: UUID(), categoryId: categoryId, ruleWorthy: false)
        let model = InboxModel()
        _ = reply(200, body: Data(#"{"items":[\#(worthy),\#(plain)],"next_cursor":null,"total":2}"#.utf8))
        await model.load(api: api)

        let item = model.items[0]
        #expect(item.isAISuggestion)
        #expect(item.suggestedMerchant == "Hertz")
        #expect(item.ruleWorthy)

        var requests = reply(200, body: Data(worthy.utf8)).makeAsyncIterator()
        let rule = try #require(await model.applySuggestion(item, api: api))
        #expect(rule.setCategoryId == categoryId)
        #expect(rule.setBucket == .business)
        #expect(rule.pattern == "HERTZ")
        let body = try #require(await requests.next().flatMap(bodyData))
        let json = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
        #expect(Set(json.keys) == ["category_id", "bucket", "reviewed", "merchant"])
        #expect(json["merchant"] as? String == "Hertz")

        #expect(await model.applySuggestion(model.items[0], api: api) == nil)
    }

    @Test func inboxItemWithoutTheNewFieldsDecodesAsHistory() throws {
        let item = try JSONDecoder.api.decode(InboxItem.self, from: Data(inboxItemJSON(id: UUID(), unmatched: false).utf8))
        #expect(item.suggestionSource == nil)
        #expect(!item.isAISuggestion)
        #expect(!item.ruleWorthy)
    }

    @Test func recategorizeWithNoCategorySendsNull() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let id = UUID()
        let model = InboxModel()
        _ = reply(200, body: Data(inboxPageJSON(ids: [id], cursor: nil, total: 1).utf8))
        await model.load(api: api)

        var requests = reply(200, body: Data(inboxItemJSON(id: id, unmatched: false).utf8)).makeAsyncIterator()
        await model.recategorize(model.items[0], categoryId: nil, bucket: .income, api: api)
        let body = try #require(await requests.next().flatMap(bodyData))
        let json = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
        #expect(Set(json.keys) == ["category_id", "bucket", "reviewed"])
        #expect(json["category_id"] is NSNull)
        #expect(json["bucket"] as? String == "income")
    }

    @Test func overlappingEditsRestoreAFailedRowBeforeItsFollower() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = (0..<4).map { _ in UUID() }
        let model = InboxModel()
        _ = reply(200, body: Data(inboxPageJSON(ids: ids, cursor: nil, total: 4).utf8))
        await model.load(api: api)

        let ok = (status: 200, body: inboxItemJSON(id: ids[1], unmatched: false, reviewed: true))
        let held = holdPatches([ids[1]: ok, ids[2]: validationError], others: { _ in "{}" })
        let failing = model.items[2]
        let succeeding = model.items[1]
        let failedEdit = Task { await model.confirm(failing, api: api) }
        await waitForPatches(1, in: held.arrived)
        let succeededEdit = Task { await model.confirm(succeeding, api: api) }
        await waitForPatches(1, in: held.arrived)
        #expect(model.items.map(\.id) == [ids[0], ids[3]])

        held.release[ids[1]]?.yield()
        await succeededEdit.value
        held.release[ids[2]]?.yield()
        await failedEdit.value
        #expect(model.items.map(\.id) == [ids[0], ids[2], ids[3]])
        #expect(model.total == 3)
        #expect(model.editError?.rowName == "HERTZ")
    }

    @Test func loadMoreSkipsRowsAlreadyListed() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = (0..<3).map { _ in UUID() }
        let first = inboxPageJSON(ids: [ids[0], ids[1]], cursor: "c1", total: 3)
        let second = inboxPageJSON(ids: [ids[1], ids[2]], cursor: nil, total: 3)
        StubURLProtocol.respond { request in
            let cursor = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "cursor" }
            return (StubURLProtocol.response(to: request, status: 200), Data((cursor == nil ? first : second).utf8))
        }
        let model = InboxModel()

        await model.load(api: api)
        await model.loadMore(api: api)
        #expect(model.items.map(\.id) == ids)
        #expect(!model.hasMore)
    }

    @Test func failedLoadMoreKeepsItemsAndSetsError() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = [UUID()]
        let model = InboxModel()
        _ = reply(200, body: Data(inboxPageJSON(ids: ids, cursor: "c1", total: 2).utf8))
        await model.load(api: api)

        _ = reply(500, body: Data(#"{"error":{"code":"internal","message":"Boom.","request_id":"r3"}}"#.utf8))
        await model.loadMore(api: api)
        #expect(model.items.map(\.id) == ids)
        #expect(model.error?.status == 500)
        #expect(model.hasMore)
        #expect(!model.isLoadingMore)
    }

    @Test func hasLoadedCountsFinishedLoadsOnly() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = InboxModel()
        #expect(!model.hasLoaded)

        let (received, receivedSignal) = AsyncStream.makeStream(of: Void.self)
        StubURLProtocol.respond { request in
            receivedSignal.yield()
            try await Task.sleep(for: .seconds(30))
            return (StubURLProtocol.response(to: request, status: 200), Data())
        }
        let cancelled = Task { await model.load(api: api) }
        for await _ in received { break }
        cancelled.cancel()
        await cancelled.value
        #expect(!model.hasLoaded)

        _ = reply(500, body: Data(#"{"error":{"code":"internal","message":"Boom.","request_id":"r4"}}"#.utf8))
        await model.load(api: api)
        #expect(model.hasLoaded)
        #expect(model.error != nil)
    }

    @Test func transactionsEndpointSendsOnlySetFilters() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        var requests = reply(200, body: Data(#"{"items":[],"next_cursor":null}"#.utf8)).makeAsyncIterator()

        _ = try await api.transactions(TransactionQuery(month: MonthRef(year: 2026, month: 10), search: "gas", bucket: .business, reviewed: false))
        let url = try #require(await requests.next()?.url)
        #expect(url.path() == "/api/transactions")
        #expect(URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems == [
            URLQueryItem(name: "month", value: "2026-10"),
            URLQueryItem(name: "q", value: "gas"),
            URLQueryItem(name: "bucket", value: "business"),
            URLQueryItem(name: "reviewed", value: "false"),
        ])
    }

    private func transactionJSON(
        id: UUID,
        bucket: String = "personal",
        categoryId: String? = nil,
        merchant: String? = nil,
        notes: String? = nil,
        reviewed: Bool = false
    ) -> String {
        let quoted = { (value: String?) in value.map { "\"\($0)\"" } ?? "null" }
        return """
        {"id":"\(uuidString(id))","account_id":"265cd02e-4a77-51ea-ad3a-4a9fbc282065","posted_date":"2026-10-08","transacted_at":null,"amount":"-85.00","description_raw":"HERTZ","merchant":\(quoted(merchant)),"category_id":\(quoted(categoryId)),"bucket":"\(bucket)","source":"manual","external_id":null,"pending":false,"reviewed":\(reviewed),"notes":\(quoted(notes)),"event_id":null,"parent_id":null,"reimbursement_id":null,"is_split":false,"created_at":"2026-10-18T20:42:06.089964Z","updated_at":"2026-10-18T20:42:06.089964Z"}
        """
    }

    private func transactionsPageJSON(_ rows: [String], cursor: String? = nil) -> Data {
        let next = cursor.map { "\"\($0)\"" } ?? "null"
        return Data(#"{"items":[\#(rows.joined(separator: ","))],"next_cursor":\#(next)}"#.utf8)
    }

    func queryItems(_ request: URLRequest) -> [URLQueryItem] {
        URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
    }

    @Test func transactionsFixtureDecodesInServerOrder() throws {
        let page = try JSONDecoder.api.decode(TransactionPage.self, from: jsonFixture("transactions-2026-10"))
        #expect(page.items.count == 11)
        #expect(page.nextCursor == nil)
        let dates = page.items.map(\.postedDate)
        #expect(dates == dates.sorted { $1 < $0 })
    }

    @Test func transactionsModelLoadSendsMonthAndOnlySetFilters() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = TransactionsModel()
        model.filter.month = MonthRef(year: 2026, month: 10)
        var requests = reply(200, body: transactionsPageJSON([])).makeAsyncIterator()

        await model.load(api: api)
        let plain = try #require(await requests.next())
        #expect(queryItems(plain) == [URLQueryItem(name: "month", value: "2026-10")])

        let account = UUID()
        let category = UUID()
        model.filter.search = "costco"
        model.filter.bucket = .business
        model.filter.accountId = account
        model.filter.categoryId = category
        await model.load(api: api)
        let full = try #require(await requests.next())
        #expect(queryItems(full) == [
            URLQueryItem(name: "month", value: "2026-10"),
            URLQueryItem(name: "q", value: "costco"),
            URLQueryItem(name: "bucket", value: "business"),
            URLQueryItem(name: "account_id", value: uuidString(account)),
            URLQueryItem(name: "category_id", value: uuidString(category)),
        ])
    }

    @Test func transactionsModelFollowsNextCursor() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = (0..<3).map { _ in UUID() }
        let first = transactionsPageJSON(ids[0..<2].map { transactionJSON(id: $0) }, cursor: "c1")
        let second = transactionsPageJSON([transactionJSON(id: ids[1]), transactionJSON(id: ids[2])])
        let (seen, seenSignal) = AsyncStream.makeStream(of: URLRequest.self)
        StubURLProtocol.respond { request in
            seenSignal.yield(request)
            let cursor = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "cursor" }
            return (StubURLProtocol.response(to: request, status: 200), cursor == nil ? first : second)
        }
        var requests = seen.makeAsyncIterator()
        let model = TransactionsModel()
        model.filter.month = MonthRef(year: 2026, month: 10)

        await model.load(api: api)
        #expect(model.hasMore)
        await model.loadMore(api: api)
        _ = await requests.next()
        let paged = try #require(await requests.next())
        #expect(queryItems(paged) == [URLQueryItem(name: "month", value: "2026-10"), URLQueryItem(name: "cursor", value: "c1")])
        #expect(model.items.map(\.id) == ids)
        #expect(!model.hasMore)
        await model.loadMore(api: api)
        #expect(model.items.count == 3)
    }

    @Test func transactionsModelIgnoresResponseForSupersededFilter() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let october = UUID()
        let september = UUID()
        let (arrived, arrivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let octoberPage = transactionsPageJSON([transactionJSON(id: october)])
        let septemberPage = transactionsPageJSON([transactionJSON(id: september)])
        StubURLProtocol.respond { request in
            let month = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "month" }?.value
            if month == "2026-10" {
                arrivedSignal.yield()
                for await _ in release { break }
                return (StubURLProtocol.response(to: request, status: 200), octoberPage)
            }
            return (StubURLProtocol.response(to: request, status: 200), septemberPage)
        }
        let model = TransactionsModel()
        model.filter.month = MonthRef(year: 2026, month: 10)
        let stale = Task { await model.load(api: api) }
        for await _ in arrived { break }

        model.filter.month = MonthRef(year: 2026, month: 9)
        await model.load(api: api)
        releaseSignal.yield()
        await stale.value
        #expect(model.items.map(\.id) == [september])
        #expect(model.error == nil)
    }

    @Test func transactionsModelDropsRowsOnFilterChangeButKeepsThemOnSameFilterReload() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let october = UUID()
        let septemberPage = transactionsPageJSON([transactionJSON(id: UUID())])
        let (arrived, arrivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let octoberPage = transactionsPageJSON([transactionJSON(id: october)])
        let model = TransactionsModel()
        model.filter.month = MonthRef(year: 2026, month: 10)
        _ = reply(200, body: octoberPage)
        await model.load(api: api)
        #expect(model.items.map(\.id) == [october])

        StubURLProtocol.respond { request in
            arrivedSignal.yield()
            for await _ in release { break }
            return (StubURLProtocol.response(to: request, status: 200), septemberPage)
        }
        let same = Task { await model.load(api: api) }
        for await _ in arrived { break }
        #expect(model.items.map(\.id) == [october])
        #expect(model.hasLoaded)
        releaseSignal.yield()
        await same.value

        model.filter.month = MonthRef(year: 2026, month: 9)
        let other = Task { await model.load(api: api) }
        for await _ in arrived { break }
        #expect(model.items.isEmpty)
        #expect(!model.hasLoaded)
        releaseSignal.yield()
        await other.value
        #expect(model.items.count == 1)
        #expect(model.hasLoaded)
    }

    @Test func recategorizeUpdatesRowInPlaceWithServerFields() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = (0..<2).map { _ in UUID() }
        let category = UUID()
        let model = TransactionsModel()
        _ = reply(200, body: transactionsPageJSON(ids.map { transactionJSON(id: $0) }))
        await model.load(api: api)

        let saved = transactionJSON(id: ids[0], bucket: "business", categoryId: uuidString(category), merchant: "Hertz Corp")
        var requests = reply(200, body: Data(saved.utf8)).makeAsyncIterator()
        await model.recategorize(model.items[0], categoryId: category, bucket: .business, api: api)

        let json = try #require(await requests.next().flatMap(bodyData).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] })
        #expect((json["category_id"] as? String)?.lowercased() == uuidString(category))
        #expect(json["bucket"] as? String == "business")
        #expect(model.items.map(\.id) == ids)
        #expect(model.items[0].bucket == .business)
        #expect(model.items[0].categoryId == category)
        #expect(model.items[0].merchant == "Hertz Corp")
        #expect(model.transaction(id: ids[0])?.merchant == "Hertz Corp")
        #expect(model.editError == nil)
    }

    @Test func failedTransactionEditRestoresRowAndNamesIt() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = (0..<2).map { _ in UUID() }
        let model = TransactionsModel()
        _ = reply(200, body: transactionsPageJSON(ids.map { transactionJSON(id: $0) }))
        await model.load(api: api)
        let before = model.items[1]

        let held = holdPatches([ids[1]: validationError], others: { _ in "{}" })
        let inFlight = Task { _ = await model.recategorize(before, categoryId: UUID(), bucket: .income, api: api) }
        await waitForPatches(1, in: held.arrived)
        #expect(model.items[1].bucket == .income)

        held.release[ids[1]]?.yield()
        await inFlight.value
        #expect(model.items[1] == before)
        #expect(model.editError?.message.contains("Nope.") == true)
        #expect(model.editError?.rowName == "HERTZ")
    }

    @Test func loadIfNeededSkipsAnUnchangedFilterAndLoadsAChangedOne() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = TransactionsModel()
        _ = reply(200, body: transactionsPageJSON([transactionJSON(id: UUID())]))
        await model.loadIfNeeded(api: api)
        #expect(model.items.count == 1)

        _ = reply(500, body: Data(#"{"detail":"Boom."}"#.utf8))
        await model.loadIfNeeded(api: api)
        #expect(model.items.count == 1)
        #expect(model.error == nil)

        model.filter.bucket = .business
        await model.loadIfNeeded(api: api)
        #expect(model.error != nil)
        #expect(model.items.isEmpty)
    }

    @Test func onEditedFiresOncePerSavedEditAndNotOnFailure() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let id = UUID()
        var edits = 0
        let model = TransactionsModel(onEdited: { edits += 1 })
        _ = reply(200, body: transactionsPageJSON([transactionJSON(id: id)]))
        await model.load(api: api)
        #expect(edits == 0)

        _ = reply(200, body: Data(transactionJSON(id: id, reviewed: true).utf8))
        await model.setReviewed(model.items[0], true, api: api)
        #expect(edits == 1)

        _ = reply(422, body: Data(validationError.body.utf8))
        await model.setReviewed(model.items[0], false, api: api)
        #expect(edits == 1)
        #expect(model.editError != nil)
    }

    @Test func editMovingRowOutOfBucketFilterRemovesIt() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = (0..<2).map { _ in UUID() }
        let model = TransactionsModel()
        model.filter.bucket = .business
        _ = reply(200, body: transactionsPageJSON(ids.map { transactionJSON(id: $0, bucket: "business") }))
        await model.load(api: api)

        _ = reply(200, body: Data(transactionJSON(id: ids[0], bucket: "personal").utf8))
        await model.recategorize(model.items[0], categoryId: nil, bucket: .personal, api: api)
        #expect(model.items.map(\.id) == [ids[1]])

        _ = reply(200, body: Data(transactionJSON(id: ids[1], bucket: "business", reviewed: true).utf8))
        await model.setReviewed(model.items[0], true, api: api)
        #expect(model.items.map(\.id) == [ids[1]])
        #expect(model.items[0].reviewed)
    }

    @Test func transactionsRefreshInFlightDuringAnEditKeepsTheEdit() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let id = UUID()
        let model = TransactionsModel()
        _ = reply(200, body: transactionsPageJSON([transactionJSON(id: id)]))
        await model.load(api: api)
        let edited = transactionJSON(id: id, notes: "Toll")
        let held = holdLoads(
            page: transactionsPageJSON([transactionJSON(id: id)]),
            reloaded: transactionsPageJSON([edited]),
            other: { _ in (200, edited) }
        )

        let slow = Task { await model.load(api: api) }
        for await _ in held.arrived { break }
        await model.setNotes(model.items[0], "Toll", api: api)
        held.release.yield()
        await slow.value

        #expect(model.items.map(\.id) == [id])
        #expect(model.items[0].notes == "Toll")
        #expect(model.editError == nil)
    }

    @Test func blankNotesAndReviewedSendExactBodies() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let id = UUID()
        let model = TransactionsModel()
        _ = reply(200, body: transactionsPageJSON([transactionJSON(id: id, notes: "old")]))
        await model.load(api: api)

        var requests = reply(200, body: Data(transactionJSON(id: id).utf8)).makeAsyncIterator()
        await model.setNotes(model.items[0], "  ", api: api)
        let notes = try #require(await requests.next().flatMap(bodyData))
        #expect(String(decoding: notes, as: UTF8.self) == #"{"notes":null}"#)
        #expect(model.items[0].notes == nil)

        await model.setReviewed(model.items[0], false, api: api)
        let reviewed = try #require(await requests.next().flatMap(bodyData))
        #expect(String(decoding: reviewed, as: UTF8.self) == #"{"reviewed":false}"#)
    }

    @Test func reloadDuringSucceededTransactionEditAppliesServerReplyToReloadedRow() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let id = UUID()
        let page = transactionsPageJSON([transactionJSON(id: id)])
        let model = TransactionsModel()
        _ = reply(200, body: page)
        await model.load(api: api)

        let held = holdPatches([id: (status: 200, body: transactionJSON(id: id, reviewed: true))], others: { _ in String(decoding: page, as: UTF8.self) })
        let transaction = model.items[0]
        let inFlight = Task { await model.setReviewed(transaction, true, api: api) }
        await waitForPatches(1, in: held.arrived)
        await model.load(api: api)
        #expect(!model.items[0].reviewed)

        held.release[id]?.yield()
        await inFlight.value
        #expect(model.items.count == 1)
        #expect(model.items[0].reviewed)
    }

    @Test func reloadDuringFailedTransactionEditLeavesReloadedRowsAlone() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = (0..<2).map { _ in UUID() }
        let page = transactionsPageJSON(ids.map { transactionJSON(id: $0) })
        let model = TransactionsModel()
        _ = reply(200, body: page)
        await model.load(api: api)

        let held = holdPatches([ids[1]: validationError], others: { _ in String(decoding: page, as: UTF8.self) })
        let transaction = model.items[1]
        let inFlight = Task { _ = await model.recategorize(transaction, categoryId: nil, bucket: .income, api: api) }
        await waitForPatches(1, in: held.arrived)
        await model.load(api: api)

        held.release[ids[1]]?.yield()
        await inFlight.value
        #expect(model.items.map(\.id) == ids)
        #expect(model.items[1].bucket == .personal)
        #expect(model.editError?.rowName == "HERTZ")
    }

    @Test func refreshWithSameFilterKeepsLoadedDepth() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = (0..<102).map { _ in UUID() }
        let first = transactionsPageJSON(ids[0..<101].map { transactionJSON(id: $0) }, cursor: "c1")
        let second = transactionsPageJSON([transactionJSON(id: ids[101])])
        let everything = transactionsPageJSON(ids.map { transactionJSON(id: $0) })
        let (seen, seenSignal) = AsyncStream.makeStream(of: URLRequest.self)
        StubURLProtocol.respond { request in
            seenSignal.yield(request)
            let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
            if items.contains(where: { $0.name == "limit" }) {
                return (StubURLProtocol.response(to: request, status: 200), everything)
            }
            return (StubURLProtocol.response(to: request, status: 200), items.contains { $0.name == "cursor" } ? second : first)
        }
        var requests = seen.makeAsyncIterator()
        let model = TransactionsModel()
        model.filter.month = MonthRef(year: 2026, month: 10)

        await model.load(api: api)
        await model.loadMore(api: api)
        #expect(model.items.count == 102)
        _ = await requests.next()
        _ = await requests.next()

        await model.load(api: api)
        let refresh = try #require(await requests.next())
        #expect(queryItems(refresh).contains(URLQueryItem(name: "limit", value: "102")))
        #expect(model.items.map(\.id) == ids)
        #expect(!model.hasMore)
    }

    @Test func refreshCapsLoadedDepthAtServerMaximum() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = (0..<501).map { _ in UUID() }
        let model = TransactionsModel()
        model.filter.month = MonthRef(year: 2026, month: 10)
        let rows = ids.map { transactionJSON(id: $0) }
        var requests = reply(200, body: transactionsPageJSON(rows)).makeAsyncIterator()
        await model.load(api: api)
        await model.load(api: api)
        _ = await requests.next()
        let refresh = try #require(await requests.next())
        #expect(queryItems(refresh).contains(URLQueryItem(name: "limit", value: "500")))
    }

    @Test func failedTransactionsLoadMoreKeepsItemsAndSetsError() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = [UUID()]
        let model = TransactionsModel()
        _ = reply(200, body: transactionsPageJSON(ids.map { transactionJSON(id: $0) }, cursor: "c1"))
        await model.load(api: api)

        _ = reply(500, body: Data(#"{"error":{"code":"internal","message":"Boom.","request_id":"r6"}}"#.utf8))
        await model.loadMore(api: api)
        #expect(model.items.map(\.id) == ids)
        #expect(model.error?.status == 500)
        #expect(model.hasMore)
        #expect(!model.isLoadingMore)
    }

    @Test func loadMoreSupersededByFilterChangeIsIgnored() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let october = UUID()
        let septemberRow = UUID()
        let (arrived, arrivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let octoberFirst = transactionsPageJSON([transactionJSON(id: october)], cursor: "c1")
        let octoberMore = transactionsPageJSON([transactionJSON(id: UUID())])
        let septemberPage = transactionsPageJSON([transactionJSON(id: septemberRow)])
        StubURLProtocol.respond { request in
            let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
            if items.contains(where: { $0.name == "cursor" }) {
                arrivedSignal.yield()
                for await _ in release { break }
                return (StubURLProtocol.response(to: request, status: 200), octoberMore)
            }
            let month = items.first { $0.name == "month" }?.value
            return (StubURLProtocol.response(to: request, status: 200), month == "2026-10" ? octoberFirst : septemberPage)
        }
        let model = TransactionsModel()
        model.filter.month = MonthRef(year: 2026, month: 10)
        await model.load(api: api)
        let more = Task { await model.loadMore(api: api) }
        for await _ in arrived { break }

        model.filter.month = MonthRef(year: 2026, month: 9)
        await model.load(api: api)
        releaseSignal.yield()
        await more.value
        #expect(model.items.map(\.id) == [septemberRow])
        #expect(!model.isLoadingMore)
        #expect(model.error == nil)
    }

    @Test func editMovingRowOutOfCategoryFilterRemovesIt() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = (0..<2).map { _ in UUID() }
        let category = UUID()
        let other = UUID()
        let model = TransactionsModel()
        model.filter.categoryId = category
        _ = reply(200, body: transactionsPageJSON(ids.map { transactionJSON(id: $0, categoryId: uuidString(category)) }))
        await model.load(api: api)

        _ = reply(200, body: Data(transactionJSON(id: ids[0], categoryId: uuidString(other)).utf8))
        await model.recategorize(model.items[0], categoryId: other, bucket: .personal, api: api)
        #expect(model.items.map(\.id) == [ids[1]])
    }

    @Test func notesAndMerchantAreTrimmedInTheirBodies() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let id = UUID()
        let model = TransactionsModel()
        _ = reply(200, body: transactionsPageJSON([transactionJSON(id: id)]))
        await model.load(api: api)

        var requests = reply(200, body: Data(transactionJSON(id: id).utf8)).makeAsyncIterator()
        await model.setNotes(model.items[0], "  Lunch ", api: api)
        let notes = try #require(await requests.next().flatMap(bodyData))
        #expect(String(decoding: notes, as: UTF8.self) == #"{"notes":"Lunch"}"#)

        await model.setMerchant(model.items[0], "Hertz", api: api)
        let merchant = try #require(await requests.next().flatMap(bodyData))
        #expect(String(decoding: merchant, as: UTF8.self) == #"{"merchant":"Hertz"}"#)
    }

    @Test func searchIsTrimmedInTheQuery() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = TransactionsModel()
        model.filter.month = MonthRef(year: 2026, month: 10)
        model.filter.search = "  costco "
        var requests = reply(200, body: transactionsPageJSON([])).makeAsyncIterator()
        await model.load(api: api)
        let request = try #require(await requests.next())
        #expect(queryItems(request).contains(URLQueryItem(name: "q", value: "costco")))
    }

    @Test func failedTransactionsRefreshKeepsItemsAndSetsError() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = [UUID()]
        let model = TransactionsModel()
        _ = reply(200, body: transactionsPageJSON(ids.map { transactionJSON(id: $0) }))
        await model.load(api: api)
        #expect(model.error == nil)

        _ = reply(500, body: Data(#"{"error":{"code":"internal","message":"Boom.","request_id":"r5"}}"#.utf8))
        await model.load(api: api)
        #expect(model.items.map(\.id) == ids)
        #expect(model.error?.status == 500)
        #expect(model.hasLoaded)
    }

    @Test func uploadReceiptSendsMultipartAndDecodes() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let transactionId = UUID()
        let receiptId = UUID()
        let jpeg = Data([0xFF, 0xD8, 0x01, 0x02, 0xFF, 0xD9])
        let reply = #"{"id":"\#(receiptId.uuidString.lowercased())","transaction_id":"\#(transactionId.uuidString.lowercased())","mime":"image/jpeg","sha256":"abc123","uploaded_at":"2026-10-18T20:42:06.089964Z"}"#
        var requests = self.reply(201, body: Data(reply.utf8)).makeAsyncIterator()

        let receipt = try await api.uploadReceipt(transactionId: transactionId, jpeg: jpeg, receiptId: receiptId)
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "POST")
        #expect(request.url?.path() == "/api/transactions/\(transactionId.uuidString.lowercased())/receipts")
        let contentType = try #require(request.value(forHTTPHeaderField: "Content-Type"))
        #expect(contentType.hasPrefix("multipart/form-data; boundary="))
        let boundary = String(contentType.dropFirst("multipart/form-data; boundary=".count))
        let body = try #require(bodyData(request))
        let text = String(decoding: body, as: UTF8.self)
        #expect(text.hasPrefix("--\(boundary)\r\n"))
        #expect(text.hasSuffix("--\(boundary)--\r\n"))
        #expect(text.contains("name=\"receipt_id\"\r\n\r\n\(receiptId.uuidString.lowercased())\r\n"))
        #expect(text.contains("name=\"file\"; filename=\"receipt.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n"))
        #expect(body.range(of: jpeg) != nil)
        #expect(receipt.id == receiptId)
        #expect(receipt.transactionId == transactionId)
        #expect(receipt.mime == "image/jpeg")
    }

    @Test func uploadReceiptSurfacesUnsupportedMediaType() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let envelope = #"{"error":{"code":"unsupported_media_type","message":"Unsupported image type.","request_id":"r415"}}"#
        _ = reply(415, body: Data(envelope.utf8))

        let error = await #expect(throws: APIError.self) {
            _ = try await api.uploadReceipt(transactionId: UUID(), jpeg: Data([0xFF, 0xD8]), receiptId: UUID())
        }
        #expect(error?.status == 415)
        #expect(error?.code == "unsupported_media_type")
    }

    private func receiptReply(transactionId: String, receiptId: String) -> Data {
        Data(#"{"id":"\#(receiptId)","transaction_id":"\#(transactionId)","mime":"image/jpeg","sha256":"abc123","uploaded_at":"2026-10-18T20:42:06.089964Z"}"#.utf8)
    }

    private func pngData() -> Data {
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        return UIGraphicsImageRenderer(size: CGSize(width: 40, height: 30), format: format).pngData { context in
            UIColor.red.setFill()
            context.fill(CGRect(x: 0, y: 0, width: 40, height: 30))
        }
    }

    /// The `receipt_id` form field of a multipart upload.
    private func receiptId(in request: URLRequest) -> String? {
        let text = String(decoding: bodyData(request) ?? Data(), as: UTF8.self)
        guard let range = text.range(of: "name=\"receipt_id\"\r\n\r\n") else { return nil }
        return text[range.upperBound...].components(separatedBy: "\r\n").first
    }

    @Test func failedReceiptUploadRetriesWithTheSameReceiptId() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let transactionId = UUID()
        let png = pngData()
        let attempts = OSAllocatedUnfairLock(initialState: 0)
        let (requests, seen) = AsyncStream.makeStream(of: URLRequest.self)
        StubURLProtocol.respond { [self] request in
            seen.yield(request)
            let attempt = attempts.withLock { count -> Int in count += 1; return count }
            if attempt == 1 {
                let envelope = #"{"error":{"code":"internal","message":"Try again.","request_id":"r1"}}"#
                return (StubURLProtocol.response(to: request, status: 500), Data(envelope.utf8))
            }
            return (StubURLProtocol.response(to: request, status: 201), await receiptReply(
                transactionId: transactionId.uuidString.lowercased(), receiptId: UUID().uuidString.lowercased()))
        }
        var iterator = requests.makeAsyncIterator()
        let uploads = ReceiptUploads(doneDisplay: .seconds(60))

        await uploads.attach(to: transactionId, name: "Hertz", api: api) { png }
        guard case .failed = uploads.uploads.first?.state else {
            Issue.record("expected a failed upload, got \(String(describing: uploads.uploads.first?.state))")
            return
        }
        #expect(uploads.uploads.first?.canRetry == true)
        await uploads.retry(transactionId, api: api)
        #expect(uploads.uploads.first?.state == .done)

        let first = try #require(await iterator.next())
        let second = try #require(await iterator.next())
        let firstId = try #require(receiptId(in: first))
        #expect(firstId == receiptId(in: second))
        #expect(UUID(uuidString: firstId) != nil)
    }

    @Test func receiptUploadFailureLeavesAnotherTransactionsSuccessIntact() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let good = UUID()
        let bad = UUID()
        let png = pngData()
        StubURLProtocol.respond { [self] request in
            if request.url?.path().contains(bad.uuidString.lowercased()) == true {
                let envelope = #"{"error":{"code":"internal","message":"Nope.","request_id":"r2"}}"#
                return (StubURLProtocol.response(to: request, status: 500), Data(envelope.utf8))
            }
            return (StubURLProtocol.response(to: request, status: 201), await receiptReply(
                transactionId: good.uuidString.lowercased(), receiptId: UUID().uuidString.lowercased()))
        }
        let uploads = ReceiptUploads(doneDisplay: .seconds(60))

        await uploads.attach(to: good, name: "Good", api: api) { png }
        await uploads.attach(to: bad, name: "Bad", api: api) { png }

        #expect(uploads.uploads.map(\.id) == [good, bad])
        #expect(uploads.uploads.first?.state == .done)
        guard case .failed = uploads.uploads.last?.state else {
            Issue.record("expected the second upload to fail")
            return
        }
    }

    @Test func undecodableReceiptDataFailsWithoutARequest() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let requestCount = OSAllocatedUnfairLock(initialState: 0)
        StubURLProtocol.respond { request in
            requestCount.withLock { $0 += 1 }
            return (StubURLProtocol.response(to: request, status: 201), Data())
        }
        let uploads = ReceiptUploads(doneDisplay: .seconds(60))
        let transactionId = UUID()

        await uploads.attach(to: transactionId, name: "Hertz", api: api) { Data("not an image".utf8) }
        guard case .failed = uploads.uploads.first?.state else {
            Issue.record("expected a failed upload")
            return
        }
        #expect(uploads.uploads.first?.canRetry == false)
        await uploads.attach(to: transactionId, name: "Hertz", api: api) { nil }
        guard case .failed = uploads.uploads.first?.state else {
            Issue.record("expected a failed upload for unreadable photo")
            return
        }
        #expect(requestCount.withLock { $0 } == 0)
    }

    /// The bytes of the multipart `file` part of an upload.
    private func filePart(in request: URLRequest) -> Data? {
        guard let body = bodyData(request),
              let header = body.range(of: Data("filename=\"receipt.jpg\"".utf8)),
              let start = body[header.upperBound...].range(of: Data("\r\n\r\n".utf8))?.upperBound,
              let end = body[start...].range(of: Data("\r\n--".utf8))?.lowerBound
        else { return nil }
        return body[start..<end]
    }

    func scanPage(width: Int, height: Int) -> CGImage {
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        return UIGraphicsImageRenderer(size: CGSize(width: width, height: height), format: format).image { context in
            UIColor.white.setFill()
            context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        }.cgImage!
    }

    @Test func scannedPagesUploadAsOneStackedJPEG() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let transactionId = UUID()
        let (requests, seen) = AsyncStream.makeStream(of: URLRequest.self)
        StubURLProtocol.respond { [self] request in
            seen.yield(request)
            return (StubURLProtocol.response(to: request, status: 201), await receiptReply(
                transactionId: transactionId.uuidString.lowercased(), receiptId: UUID().uuidString.lowercased()))
        }
        var iterator = requests.makeAsyncIterator()
        let uploads = ReceiptUploads(doneDisplay: .seconds(60))

        await uploads.attach(
            to: transactionId, name: "Parking", api: api,
            scannedPages: [scanPage(width: 300, height: 400), scanPage(width: 300, height: 200)]
        )

        #expect(uploads.uploads.first?.state == .done)
        let request = try #require(await iterator.next())
        #expect(request.url?.path() == "/api/transactions/\(transactionId.uuidString.lowercased())/receipts")
        let jpeg = try #require(filePart(in: request))
        #expect(Array(jpeg.prefix(2)) == [0xFF, 0xD8])
        #expect(UIImage(data: jpeg)?.size == CGSize(width: 300, height: 600))
        #expect(receiptId(in: request).flatMap(UUID.init(uuidString:)) != nil)
    }

    @Test func emptyOrFailedScanShowsAFailureWithoutARequest() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let requestCount = OSAllocatedUnfairLock(initialState: 0)
        StubURLProtocol.respond { request in
            requestCount.withLock { $0 += 1 }
            return (StubURLProtocol.response(to: request, status: 201), Data())
        }
        let uploads = ReceiptUploads(doneDisplay: .seconds(60))
        let empty = UUID()
        let failed = UUID()

        await uploads.attach(to: empty, name: "Empty", api: api, scannedPages: [])
        uploads.scanFailed(for: failed, name: "Failed")

        #expect(uploads.uploads.map(\.id) == [empty, failed])
        for upload in uploads.uploads {
            guard case .failed = upload.state else {
                Issue.record("expected a failed entry for \(upload.name)")
                continue
            }
            #expect(upload.canRetry == false)
        }
        #expect(requestCount.withLock { $0 } == 0)
    }

    @Test func noContentDecodesEmpty() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        _ = reply(204)
        let _: APIClient.Empty = try await api.send("DELETE", "/api/things/1", body: nil as String?)
    }

    @Test func mapsErrorEnvelope() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let body = #"{"error":{"code":"validation_error","message":"Invalid request.","request_id":"req-422"}}"#
        _ = reply(422, body: Data(body.utf8), headers: ["X-Request-ID": "header-id"])

        let error = await #expect(throws: APIError.self) { let _: Home = try await api.get("/api/home") }
        #expect(error?.status == 422)
        #expect(error?.code == "validation_error")
        #expect(error?.requestId == "req-422")
    }

    @Test func fallsBackToRequestIdHeader() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        _ = reply(502, body: Data("<html>Bad gateway</html>".utf8), headers: ["X-Request-ID": "header-id"])

        let error = await #expect(throws: APIError.self) { let _: Home = try await api.get("/api/home") }
        #expect(error?.status == 502)
        #expect(error?.code == "http_502")
        #expect(error?.requestId == "header-id")
        #expect(error?.errorDescription == "Request failed (HTTP 502).")
    }

    @Test func unauthorizedSignsOut() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "expired", serverURL: server)
        let api = makeClient(session)
        _ = reply(401)

        let error = await #expect(throws: APIError.self) { let _: Home = try await api.get("/api/home") }
        #expect(error?.isUnauthorized == true)
        #expect(!session.isSignedIn)
        #expect(try Keychain.read(fixture.tokenKey) == nil)
    }

    @Test func staleUnauthorizedRetriesWithNewerToken() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "old", serverURL: server)
        let api = makeClient(session)
        let home = try jsonFixture("home-2026-10")
        let (received, receivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let sentTokens = OSAllocatedUnfairLock(initialState: [String]())
        StubURLProtocol.respond { request in
            let token = request.value(forHTTPHeaderField: "Authorization") ?? ""
            sentTokens.withLock { $0.append(token) }
            guard token == "Bearer old" else { return (StubURLProtocol.response(to: request, status: 200), home) }
            receivedSignal.yield()
            for await _ in release { break }
            return (StubURLProtocol.response(to: request, status: 401), Data())
        }

        let inFlight = Task { let _: Home = try await api.get("/api/home") }
        for await _ in received { break }
        try session.signIn(token: "new", serverURL: server)
        releaseSignal.yield()

        _ = try await inFlight.value
        #expect(sentTokens.withLock { $0 } == ["Bearer old", "Bearer new"])
        #expect(session.accessToken == "new")
        #expect(try Keychain.read(fixture.tokenKey) == "new")
    }

    @Test func cancellationThrowsCancellationError() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let (received, receivedSignal) = AsyncStream.makeStream(of: Void.self)
        StubURLProtocol.respond { request in
            receivedSignal.yield()
            try await Task.sleep(for: .seconds(30))
            return (StubURLProtocol.response(to: request, status: 204), Data())
        }

        let inFlight = Task { let _: APIClient.Empty = try await api.get("/api/home") }
        for await _ in received { break }
        inFlight.cancel()

        await #expect(throws: CancellationError.self) { try await inFlight.value }
    }

    @Test func otherTransportErrorsBecomeAPIError() async throws {
        struct Unplugged: Error {}
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        StubURLProtocol.respond { _ in throw Unplugged() }

        let error = await #expect(throws: APIError.self) { let _: Home = try await api.get("/api/home") }
        guard case .transport = error else {
            Issue.record("Expected a transport error, got \(String(describing: error))")
            return
        }
    }

    @Test func encodingFailureBecomesAPIError() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        _ = reply(204)

        let error = await #expect(throws: APIError.self) {
            let _: APIClient.Empty = try await api.send("POST", "/api/things", body: ["amount": Double.nan])
        }
        guard case .encoding = error else {
            Issue.record("Expected an encoding error, got \(String(describing: error))")
            return
        }
    }

    // MARK: Outbox sender

    private struct SentRequest: Sendable {
        let method: String
        let path: String
        let body: Data?

        /// The capture this request belongs to: the id in a create body, or the id in a receipt path.
        var transactionId: String? {
            if isReceipt { return path.split(separator: "/").dropLast().last.map { $0.lowercased() } }
            if isEventLink { return path.split(separator: "/").last.map { $0.lowercased() } }
            guard isCreate, let body, let json = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else { return nil }
            return (json["id"] as? String)?.lowercased()
        }

        /// The `description_raw` of a create body, read from the parsed JSON so a random UUID can't match it.
        var descriptionRaw: String? {
            guard isCreate, let body, let json = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else { return nil }
            return json["description_raw"] as? String
        }

        var isCreate: Bool { method == "POST" && path == "/api/transactions" }
        var isEventLink: Bool { method == "PATCH" }
        var isReceipt: Bool { path.hasSuffix("/receipts") }
    }

    private func sentRequest(_ request: URLRequest) async -> SentRequest {
        SentRequest(method: request.httpMethod ?? "", path: request.url!.path(), body: bodyData(request))
    }

    /// The logged requests that belong to `ids`, so a late request from an earlier test can't change a count.
    private func requests(_ log: OSAllocatedUnfairLock<[SentRequest]>, for ids: [UUID]) -> [SentRequest] {
        let wanted = Set(ids.map { $0.uuidString.lowercased() })
        return log.withLock { $0.filter { $0.path.hasPrefix("/api/transactions") && $0.transactionId.map(wanted.contains) == true } }
    }

    /// Answers each request through `respond` (which may throw) and records every request it saw.
    private func serve(
        _ respond: @escaping @Sendable (SentRequest) throws -> (status: Int, body: Data)
    ) -> OSAllocatedUnfairLock<[SentRequest]> {
        let log = OSAllocatedUnfairLock(initialState: [SentRequest]())
        StubURLProtocol.respond { [self] request in
            let sent = await sentRequest(request)
            log.withLock { $0.append(sent) }
            let reply = try respond(sent)
            return (StubURLProtocol.response(to: request, status: reply.status), reply.body)
        }
        return log
    }

    private func outboxCapture(_ description: String = "HERTZ", eventId: UUID? = nil) -> Capture {
        Capture(
            accountId: UUID(),
            postedDate: CalendarDate(year: 2026, month: 10, day: 8),
            amount: Money(Decimal(string: "-85")!),
            descriptionRaw: description,
            eventId: eventId
        )
    }

    /// Runs `body` with an outbox on a temporary directory (and a client on the stub), then cleans both up.
    /// `autoFlush` is off unless a test is about the outbox flushing itself.
    private func withOutbox(
        autoFlush: Bool = false,
        signedIn: Bool = false,
        _ body: (_ outbox: Outbox, _ store: OutboxStore, _ directory: URL, _ api: APIClient) async throws -> Void
    ) async throws {
        defer { try? fixture.cleanUp() }
        let directory = FileManager.default.temporaryDirectory.appending(path: "outbox-send-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: directory) }
        let session = fixture.make()
        if signedIn { try session.signIn(token: "abc", serverURL: server) }
        let api = makeClient(session)
        let store = try OutboxStore(directory: directory)
        try await body(Outbox(store: store, api: api, autoFlush: autoFlush), store, directory, api)
    }

    private var createdBody: Data { Data(inboxItemJSON(id: UUID(), unmatched: false).utf8) }

    private func files(in directory: URL) -> [String] {
        ((try? FileManager.default.contentsOfDirectory(atPath: directory.path())) ?? []).sorted()
    }

    @Test func outboxRemovesItemAfter201() async throws {
        try await withOutbox { outbox, _, directory, _ in
            let created = createdBody
            let log = serve { _ in (201, created) }
            try outbox.enqueue(outboxCapture())
            let ids = outbox.pending.map(\.id)
            #expect(outbox.pending.count == 1)

            await outbox.flush()
            #expect(requests(log, for: ids).count == 1)
            #expect(outbox.pending.isEmpty)
            #expect(outbox.failed.isEmpty)
            #expect(!outbox.isFlushing)
            #expect(files(in: directory).isEmpty)
        }
    }

    @Test func outboxRemovesItemAfter200ReplayWithoutResending() async throws {
        try await withOutbox { outbox, _, directory, _ in
            let created = createdBody
            let log = serve { _ in (200, created) }
            try outbox.enqueue(outboxCapture())
            let ids = outbox.pending.map(\.id)

            await outbox.flush()
            await outbox.flush()
            #expect(requests(log, for: ids).count == 1)
            #expect(outbox.pending.isEmpty)
            #expect(files(in: directory).isEmpty)
        }
    }

    @Test func outboxKeepsItemOnTransportErrorAndResendsSameIdAndBody() async throws {
        try await withOutbox { outbox, _, _, _ in
            let created = createdBody
            let offline = OSAllocatedUnfairLock(initialState: true)
            let log = serve { _ in
                if offline.withLock({ $0 }) { throw URLError(.notConnectedToInternet) }
                return (201, created)
            }
            let first = try outbox.enqueue(outboxCapture("first")).capture
            try outbox.enqueue(outboxCapture("second"))
            let ids = outbox.pending.map(\.id)

            await outbox.flush()
            #expect(requests(log, for: ids).count == 1)
            #expect(outbox.pending.count == 2)
            #expect(outbox.failed.isEmpty)

            offline.withLock { $0 = false }
            await outbox.flush()
            let sent = requests(log, for: ids)
            #expect(sent.count == 3)
            let firstJSON = try #require(sent[0].body.flatMap { try? JSONSerialization.jsonObject(with: $0) as? NSDictionary })
            let json = try #require(sent[1].body.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] })
            #expect(firstJSON == json as NSDictionary)
            #expect((json["id"] as? String)?.lowercased() == first.id.uuidString.lowercased())
            #expect(outbox.pending.isEmpty)
        }
    }

    @Test func outboxConcurrentFlushesPostEachCaptureOnce() async throws {
        try await withOutbox { outbox, _, _, _ in
            let created = createdBody
            let count = OSAllocatedUnfairLock(initialState: 0)
            let mine = OSAllocatedUnfairLock(initialState: Set<String>())
            let (arrived, arrivedSignal) = AsyncStream.makeStream(of: Void.self)
            let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
            StubURLProtocol.respond { [self] request in
                guard await sentRequest(request).transactionId.map({ id in mine.withLock { $0.contains(id) } }) == true else {
                    return (StubURLProtocol.response(to: request, status: 201), created)
                }
                count.withLock { $0 += 1 }
                arrivedSignal.yield()
                for await _ in release { break }
                return (StubURLProtocol.response(to: request, status: 201), created)
            }
            try outbox.enqueue(outboxCapture("a"))
            try outbox.enqueue(outboxCapture("b"))
            let ownIds = Set(outbox.pending.map { $0.id.uuidString.lowercased() })
            mine.withLock { $0 = ownIds }

            let running = Task { await outbox.flush() }
            for await _ in arrived { break }
            #expect(outbox.isFlushing)
            await outbox.flush()
            #expect(count.withLock { $0 } == 1)

            releaseSignal.yield()
            releaseSignal.yield()
            await running.value
            #expect(count.withLock { $0 } == 2)
            #expect(outbox.pending.isEmpty)
            #expect(!outbox.isFlushing)
        }
    }

    @Test func successfulRequestFlushesThePendingCaptureOnceWithoutLooping() async throws {
        try await withOutbox { outbox, _, _, api in
            let created = createdBody
            let home = Data(#"{"month":"2026-10","spending_by_bucket":{"buckets":[],"total":"0.00"},"owed_to_you":"0.00","income":{"expected":"0.00","received":"0.00"},"bills_due":[],"last_updated":null}"#.utf8)
            let hookCalls = OSAllocatedUnfairLock(initialState: 0)
            let flushes = OSAllocatedUnfairLock(initialState: [Task<Void, Never>]())
            api.onSuccess = {
                hookCalls.withLock { $0 += 1 }
                let flush = Task { await outbox.flush() }
                flushes.withLock { $0.append(flush) }
            }
            let log = serve { sent in
                sent.path == "/api/home" ? (200, home) : (201, created)
            }
            try outbox.enqueue(outboxCapture())
            let ids = outbox.pending.map(\.id)

            _ = try await api.home(month: MonthRef(year: 2026, month: 10))
            while let flush = flushes.withLock({ $0.popLast() }) { await flush.value }

            let sent = requests(log, for: ids)
            #expect(sent.count == 1)
            #expect(sent.first?.isCreate == true)
            #expect(hookCalls.withLock { $0 } == 1)
            #expect(outbox.pending.isEmpty)
        }
    }

    @Test func outboxMarksRejectedItemFailedAndContinuesWithTheNext() async throws {
        try await withOutbox { outbox, _, directory, _ in
            let created = createdBody
            let envelope = Data(#"{"error":{"code":"validation_error","message":"Amount is too large.","request_id":"r1"}}"#.utf8)
            let rejecting = OSAllocatedUnfairLock(initialState: true)
            let log = serve { sent in
                let bad = String(decoding: sent.body ?? Data(), as: UTF8.self).contains("BAD")
                if bad && rejecting.withLock({ $0 }) { return (422, envelope) }
                return (201, created)
            }
            let bad = try outbox.enqueue(outboxCapture("BAD"))
            try outbox.enqueue(outboxCapture("GOOD"))
            let ids = outbox.pending.map(\.id)

            await outbox.flush()
            #expect(requests(log, for: ids).count == 2)
            #expect(outbox.pending.isEmpty)
            #expect(outbox.failed.map(\.id) == [bad.id])
            #expect(outbox.failed.first?.failure == "Amount is too large.")

            await outbox.flush()
            #expect(requests(log, for: ids).count == 2)

            rejecting.withLock { $0 = false }
            try outbox.retry(bad.id)
            #expect(outbox.pending.map(\.id) == [bad.id])
            #expect(outbox.failed.isEmpty)
            await outbox.flush()
            #expect(requests(log, for: ids).count == 3)
            #expect(outbox.pending.isEmpty)
            #expect(files(in: directory).isEmpty)
        }
    }

    @Test func outboxDiscardDeletesAFailedItem() async throws {
        try await withOutbox { outbox, _, directory, _ in
            _ = serve { _ in (404, Data(#"{"error":{"code":"not_found","message":"No such account.","request_id":"r2"}}"#.utf8)) }
            let item = try outbox.enqueue(outboxCapture(), receiptJPEG: Data([0xFF, 0xD8, 0xFF, 0xD9]))

            await outbox.flush()
            #expect(outbox.failed.first?.failure == "No such account.")

            try outbox.discard(item.id)
            #expect(outbox.failed.isEmpty)
            #expect(files(in: directory).isEmpty)
        }
    }

    @Test func outboxRetriesOnlyTheReceiptAfterItFailsFollowingA201() async throws {
        try await withOutbox { outbox, store, directory, _ in
            let created = createdBody
            let uploaded = receiptReply(transactionId: UUID().uuidString.lowercased(), receiptId: UUID().uuidString.lowercased())
            let receiptDown = OSAllocatedUnfairLock(initialState: true)
            let log = serve { sent in
                if sent.isCreate { return (201, created) }
                if receiptDown.withLock({ $0 }) { return (500, Data(#"{"error":{"code":"internal","message":"Try again.","request_id":"r3"}}"#.utf8)) }
                return (201, uploaded)
            }
            let item = try outbox.enqueue(outboxCapture(), receiptJPEG: Data([0xFF, 0xD8, 0xFF, 0xD9]))
            let receiptId = try #require(item.capture.receiptId)
            let ids = outbox.pending.map(\.id)

            await outbox.flush()
            #expect(requests(log, for: ids).count == 2)
            #expect(outbox.pending.map(\.id) == [item.id])
            #expect(try store.allItems().items.first?.rowSent == true)
            #expect(files(in: directory).count == 2)

            receiptDown.withLock { $0 = false }
            await outbox.flush()
            let sent = requests(log, for: ids)
            #expect(sent.count == 3)
            #expect(sent.filter(\.isCreate).count == 1)
            let uploads = sent.filter(\.isReceipt)
            #expect(uploads.count == 2)
            #expect(uploads.compactMap { $0.body.flatMap { receiptIdField(in: $0) } } == Array(repeating: receiptId.uuidString.lowercased(), count: 2))
            #expect(outbox.pending.isEmpty)
            #expect(files(in: directory).isEmpty)
        }
    }

    private func eventBody(_ request: SentRequest) throws -> [String: String] {
        try #require(request.body.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: String] })
    }

    @Test func outboxLinksTheEventWithAPatchAfterTheCreateThenRemovesTheItem() async throws {
        try await withOutbox { outbox, _, directory, _ in
            let created = createdBody
            let log = serve { _ in (201, created) }
            let eventId = UUID()
            let item = try outbox.enqueue(outboxCapture(eventId: eventId))

            await outbox.flush()
            let sent = requests(log, for: [item.id])
            #expect(sent.map(\.method) == ["POST", "PATCH"])
            #expect(sent[1].path == "/api/transactions/\(item.id.uuidString.lowercased())")
            #expect(try eventBody(sent[1]).mapValues { $0.lowercased() } == ["event_id": eventId.uuidString.lowercased()])
            let create = try #require(sent[0].body.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] })
            #expect(create["event_id"] == nil)
            #expect(outbox.pending.isEmpty)
            #expect(files(in: directory).isEmpty)
        }
    }

    @Test func outboxRetriesOnlyTheEventLinkAfterItFailsFollowingA201() async throws {
        try await withOutbox { outbox, store, directory, _ in
            let created = createdBody
            let linkDown = OSAllocatedUnfairLock(initialState: true)
            let log = serve { sent in
                if sent.isCreate { return (201, created) }
                if linkDown.withLock({ $0 }) { return (500, Data(#"{"error":{"code":"internal","message":"Try again.","request_id":"r5"}}"#.utf8)) }
                return (200, created)
            }
            let item = try outbox.enqueue(outboxCapture(eventId: UUID()))

            await outbox.flush()
            #expect(requests(log, for: [item.id]).map(\.method) == ["POST", "PATCH"])
            #expect(outbox.pending.map(\.id) == [item.id])
            #expect(outbox.pending.first?.lastError == "Try again.")
            #expect(try store.allItems().items.first?.rowSent == true)
            #expect(try store.allItems().items.first?.eventLinked == false)

            linkDown.withLock { $0 = false }
            await outbox.flush()
            #expect(requests(log, for: [item.id]).map(\.method) == ["POST", "PATCH", "PATCH"])
            #expect(outbox.pending.isEmpty)
            #expect(files(in: directory).isEmpty)
        }
    }

    @Test func outboxSendsTheReceiptWhenTheEventIsGone() async throws {
        try await withOutbox { outbox, _, _, _ in
            let created = createdBody
            let uploaded = receiptReply(transactionId: UUID().uuidString.lowercased(), receiptId: UUID().uuidString.lowercased())
            let log = serve { sent in
                if sent.isCreate { return (201, created) }
                if sent.isReceipt { return (201, uploaded) }
                return (404, Data(#"{"error":{"code":"not_found","message":"No such event.","request_id":"r6"}}"#.utf8))
            }
            let item = try outbox.enqueue(
                outboxCapture(eventId: UUID()),
                receiptJPEG: Data([0xFF, 0xD8, 0xFF, 0xD9])
            )

            await outbox.flush()
            #expect(requests(log, for: [item.id]).map(\.method) == ["POST", "PATCH", "POST"])
            #expect(outbox.pending.isEmpty)
            #expect(outbox.failed.isEmpty)
            #expect(outbox.notices.map(\.id) == [item.id])
            #expect(outbox.notices.first?.title == "HERTZ")
            await outbox.flush()
            #expect(requests(log, for: [item.id]).count == 3)
        }
    }

    @Test func aDroppedEventLeavesANoticeThatOutlivesTheItemAndNeverBecomesLastError() async throws {
        try await withOutbox { outbox, store, _, _ in
            let created = createdBody
            let uploaded = receiptReply(transactionId: UUID().uuidString.lowercased(), receiptId: UUID().uuidString.lowercased())
            let receiptDown = OSAllocatedUnfairLock(initialState: true)
            _ = serve { sent in
                if sent.isCreate { return (201, created) }
                if sent.isReceipt {
                    if receiptDown.withLock({ $0 }) { throw URLError(.notConnectedToInternet) }
                    return (201, uploaded)
                }
                return (404, Data(#"{"error":{"code":"not_found","message":"No such event.","request_id":"r8"}}"#.utf8))
            }
            let item = try outbox.enqueue(outboxCapture(eventId: UUID()), receiptJPEG: Data([0xFF, 0xD8, 0xFF, 0xD9]))

            await outbox.flush()
            let stored = try #require(try store.allItems().items.first)
            #expect(stored.eventLinked)
            #expect(stored.lastError == nil)
            #expect(outbox.notices.count == 1)

            receiptDown.withLock { $0 = false }
            await outbox.flush()
            #expect(outbox.pending.isEmpty)
            let notice = try #require(outbox.notices.first)
            #expect(notice.id == item.id)
            #expect(notice.message == "Saved without its event (the event or transaction no longer exists).")

            outbox.dismissNotice(notice.id)
            #expect(outbox.notices.isEmpty)
        }
    }

    @Test func outboxSendsCreateThenEventLinkThenReceipt() async throws {
        try await withOutbox { outbox, _, directory, _ in
            let created = createdBody
            let uploaded = receiptReply(transactionId: UUID().uuidString.lowercased(), receiptId: UUID().uuidString.lowercased())
            let log = serve { sent in
                if sent.isCreate { return (201, created) }
                if sent.isReceipt { return (201, uploaded) }
                return (200, created)
            }
            let item = try outbox.enqueue(
                outboxCapture(eventId: UUID()),
                receiptJPEG: Data([0xFF, 0xD8, 0xFF, 0xD9])
            )

            await outbox.flush()
            let sent = requests(log, for: [item.id])
            #expect(sent.map(\.method) == ["POST", "PATCH", "POST"])
            #expect(sent.map(\.isReceipt) == [false, false, true])
            #expect(outbox.pending.isEmpty)
            #expect(files(in: directory).isEmpty)
        }
    }

    @Test func outboxSendsNoPatchWithoutAnEvent() async throws {
        try await withOutbox { outbox, _, _, _ in
            let created = createdBody
            let log = serve { _ in (201, created) }
            let item = try outbox.enqueue(outboxCapture())

            await outbox.flush()
            #expect(requests(log, for: [item.id]).map(\.method) == ["POST"])
        }
    }

    @Test func outboxStopsAndKeepsEverythingOn401() async throws {
        try await withOutbox(signedIn: true) { outbox, _, directory, _ in
            let log = serve { _ in (401, Data(#"{"error":{"code":"unauthorized","message":"Not signed in.","request_id":"r4"}}"#.utf8)) }
            try outbox.enqueue(outboxCapture("a"))
            try outbox.enqueue(outboxCapture("b"))
            let ids = outbox.pending.map(\.id)

            await outbox.flush()
            #expect(requests(log, for: ids).count == 1)
            #expect(outbox.pending.count == 2)
            #expect(outbox.failed.isEmpty)
            #expect(files(in: directory).count == 2)
        }
    }

    @Test func outboxRecordsA500AndSendsTheNextItem() async throws {
        try await withOutbox { outbox, store, _, _ in
            let created = createdBody
            let envelope = Data(#"{"error":{"code":"internal","message":"Try again.","request_id":"r5"}}"#.utf8)
            let broken = OSAllocatedUnfairLock(initialState: true)
            let log = serve { sent in
                let isA = sent.descriptionRaw == "AAA"
                return isA && broken.withLock({ $0 }) ? (500, envelope) : (201, created)
            }
            let a = try outbox.enqueue(outboxCapture("AAA"))
            let b = try outbox.enqueue(outboxCapture("BBB"))
            let ids = [a.id, b.id]

            await outbox.flush()
            #expect(requests(log, for: ids).count == 2)
            #expect(outbox.pending.map(\.id) == [a.id])
            #expect(outbox.pending.first?.lastError == "Try again.")
            #expect(outbox.pending.first?.attempts == 1)
            #expect(outbox.failed.isEmpty)
            let stored = try #require(try store.allItems().items.first)
            #expect(stored.lastError == "Try again.")
            #expect(stored.attempts == 1)

            await outbox.flush()
            #expect(requests(log, for: ids).count == 3)
            #expect(outbox.pending.first?.attempts == 2)

            broken.withLock { $0 = false }
            await outbox.flush()
            #expect(requests(log, for: ids).count == 4)
            #expect(outbox.pending.isEmpty)
        }
    }

    @Test func outboxRecordsAnUndecodable2xxAndSendsTheNextItem() async throws {
        try await withOutbox { outbox, _, _, _ in
            let created = createdBody
            let log = serve { sent in
                let isA = sent.descriptionRaw == "AAA"
                return isA ? (201, Data(#"{"unexpected":true}"#.utf8)) : (201, created)
            }
            let a = try outbox.enqueue(outboxCapture("AAA"))
            let b = try outbox.enqueue(outboxCapture("BBB"))

            await outbox.flush()
            #expect(requests(log, for: [a.id, b.id]).count == 2)
            #expect(outbox.pending.map(\.id) == [a.id])
            #expect(outbox.pending.first?.lastError == "The server's reply couldn't be read.")
        }
    }

    @Test func outboxSkips429ButStopsOnTransportErrorAndKeepsEverything() async throws {
        try await withOutbox { outbox, _, _, _ in
            let throttled = OSAllocatedUnfairLock(initialState: true)
            let log = serve { _ in
                if throttled.withLock({ $0 }) { return (429, Data()) }
                throw URLError(.notConnectedToInternet)
            }
            let a = try outbox.enqueue(outboxCapture("a"))
            let b = try outbox.enqueue(outboxCapture("b"))

            await outbox.flush()
            #expect(requests(log, for: [a.id, b.id]).count == 2)
            #expect(outbox.pending.count == 2)
            #expect(outbox.failed.isEmpty)

            throttled.withLock { $0 = false }
            await outbox.flush()
            #expect(requests(log, for: [a.id, b.id]).count == 3)
            #expect(outbox.pending.count == 2)
        }
    }

    @Test func outboxStopsAndReportsAStorageProblemWhenItCannotRecordProgress() async throws {
        try await withOutbox { outbox, _, directory, _ in
            let created = createdBody
            let log = serve { _ in (201, created) }
            let item = try outbox.enqueue(outboxCapture())
            try FileManager.default.setAttributes([.posixPermissions: 0o555], ofItemAtPath: directory.path())
            defer { try? FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: directory.path()) }

            await outbox.flush()
            #expect(requests(log, for: [item.id]).count == 1)
            #expect(outbox.pending.map(\.id) == [item.id])
            #expect(outbox.storageProblem != nil)
            #expect(!outbox.isFlushing)

            try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: directory.path())
            await outbox.flush()
            #expect(outbox.pending.isEmpty)
            #expect(outbox.storageProblem == nil)
        }
    }

    @Test func outboxKeepsItsLastKnownQueueWhenTheFilesCannotBeRead() async throws {
        try await withOutbox { outbox, _, directory, _ in
            let created = createdBody
            let log = serve { _ in (201, created) }
            let item = try outbox.enqueue(outboxCapture())
            let json = directory.appending(path: "\(item.id.uuidString.lowercased()).json")
            try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: json.path())
            defer { try? FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: json.path()) }

            await outbox.flush()
            #expect(requests(log, for: [item.id]).isEmpty)
            #expect(outbox.pending.map(\.id) == [item.id])
            #expect(outbox.storageProblem != nil)

            try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: json.path())
            await outbox.flush()
            #expect(outbox.pending.isEmpty)
            #expect(outbox.storageProblem == nil)
        }
    }

    @Test func outboxShowsAnUndecodableFileAsUnreadableAndDiscardRemovesIt() async throws {
        try await withOutbox { outbox, _, directory, api in
            let id = UUID()
            let file = directory.appending(path: "\(id.uuidString.lowercased()).json")
            try Data("garbage".utf8).write(to: file)
            let reopened = Outbox(store: try OutboxStore(directory: directory), api: api, autoFlush: false)

            #expect(reopened.unreadable == [id])
            #expect(reopened.pending.isEmpty)

            try reopened.discard(id)
            #expect(reopened.unreadable.isEmpty)
            #expect(files(in: directory).isEmpty)
        }
    }

    @Test func outboxEnqueueStartsAFlushOnItsOwn() async throws {
        try await withOutbox(autoFlush: true) { outbox, _, directory, _ in
            let created = createdBody
            let log = serve { _ in (201, created) }
            let item = try outbox.enqueue(outboxCapture())

            await outbox.scheduledFlush?.value
            #expect(requests(log, for: [item.id]).count == 1)
            #expect(outbox.pending.isEmpty)
            #expect(files(in: directory).isEmpty)
        }
    }

    @Test func outboxRetryStartsAFlushOnItsOwn() async throws {
        try await withOutbox(autoFlush: true) { outbox, _, _, _ in
            let created = createdBody
            let envelope = Data(#"{"error":{"code":"validation_error","message":"Nope.","request_id":"r6"}}"#.utf8)
            let rejecting = OSAllocatedUnfairLock(initialState: true)
            let log = serve { _ in rejecting.withLock { $0 } ? (422, envelope) : (201, created) }
            let item = try outbox.enqueue(outboxCapture())
            await outbox.scheduledFlush?.value
            #expect(outbox.failed.map(\.id) == [item.id])

            rejecting.withLock { $0 = false }
            try outbox.retry(item.id)
            await outbox.scheduledFlush?.value
            #expect(requests(log, for: [item.id]).count == 2)
            #expect(outbox.failed.isEmpty)
            #expect(outbox.pending.isEmpty)
        }
    }

    @Test func outboxSendsACaptureQueuedWhileAFlushIsRunning() async throws {
        try await withOutbox { outbox, _, _, _ in
            let created = createdBody
            let count = OSAllocatedUnfairLock(initialState: 0)
            let mine = OSAllocatedUnfairLock(initialState: Set<String>())
            let (arrived, arrivedSignal) = AsyncStream.makeStream(of: Void.self)
            let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
            StubURLProtocol.respond { [self] request in
                guard await sentRequest(request).transactionId.map({ id in mine.withLock { $0.contains(id) } }) == true else {
                    return (StubURLProtocol.response(to: request, status: 201), created)
                }
                count.withLock { $0 += 1 }
                arrivedSignal.yield()
                for await _ in release { break }
                return (StubURLProtocol.response(to: request, status: 201), created)
            }
            let first = try outbox.enqueue(outboxCapture("first"))
            let firstId = first.id.uuidString.lowercased()
            mine.withLock { $0 = [firstId] }

            let running = Task { await outbox.flush() }
            for await _ in arrived { break }
            let late = try outbox.enqueue(outboxCapture("late"))
            let lateId = late.id.uuidString.lowercased()
            mine.withLock { _ = $0.insert(lateId) }
            await outbox.flush()
            #expect(count.withLock { $0 } == 1)

            releaseSignal.yield()
            releaseSignal.yield()
            await running.value
            #expect(count.withLock { $0 } == 2)
            #expect(outbox.pending.isEmpty)
            #expect(!outbox.isFlushing)
        }
    }

    @Test func outboxWithUncreatableStorageRefusesCapturesWithoutCrashing() async throws {
        defer { try? fixture.cleanUp() }
        let blocker = FileManager.default.temporaryDirectory.appending(path: "outbox-blocker-\(UUID().uuidString)")
        try Data().write(to: blocker)
        defer { try? FileManager.default.removeItem(at: blocker) }
        let outbox = Outbox.makeDefault(api: makeClient(fixture.make()), directory: blocker.appending(path: "Outbox"))

        #expect(outbox.storageProblem != nil)
        #expect(throws: OutboxError.self) { try outbox.enqueue(outboxCapture()) }
        #expect(OutboxError.storageUnavailable.localizedDescription == "Captures can't be saved on this device right now.")
        #expect(outbox.pending.isEmpty)
        await outbox.flush()
    }

    @Test func outboxMarksItemFailedWhenItsReceiptFileIsMissing() async throws {
        try await withOutbox { outbox, _, directory, _ in
            let created = createdBody
            _ = serve { _ in (201, created) }
            let item = try outbox.enqueue(outboxCapture(), receiptJPEG: Data([0xFF, 0xD8, 0xFF, 0xD9]))
            let id = item.id.uuidString.lowercased()
            try FileManager.default.removeItem(at: directory.appending(path: "\(id).jpg"))

            await outbox.flush()
            #expect(outbox.pending.isEmpty)
            #expect(outbox.failed.map(\.id) == [item.id])
            #expect(outbox.failed.first?.failure == "The receipt image is missing.")
            #expect(files(in: directory) == ["\(id).json"])
        }
    }

    @Test func outboxStopsAndKeepsTheItemWhenItsReceiptFileIsUnreadable() async throws {
        try await withOutbox { outbox, _, directory, _ in
            let created = createdBody
            let log = serve { _ in (201, created) }
            let item = try outbox.enqueue(outboxCapture(), receiptJPEG: Data([0xFF, 0xD8, 0xFF, 0xD9]))
            let id = item.id.uuidString.lowercased()
            let ids = outbox.pending.map(\.id)
            let image = directory.appending(path: "\(id).jpg")
            try FileManager.default.removeItem(at: image)
            try FileManager.default.createDirectory(at: image, withIntermediateDirectories: false)

            await outbox.flush()
            #expect(requests(log, for: ids).filter(\.isReceipt).count == 0)
            #expect(outbox.pending.map(\.id) == [item.id])
            #expect(outbox.failed.isEmpty)
            #expect(files(in: directory) == ["\(id).jpg", "\(id).json"])
        }
    }

    private func receiptIdField(in body: Data) -> String? {
        let text = String(decoding: body, as: UTF8.self)
        guard let range = text.range(of: "name=\"receipt_id\"\r\n\r\n") else { return nil }
        return text[range.upperBound...].components(separatedBy: "\r\n").first
    }

    // MARK: Categories

    private func categoryJSON(id: UUID, name: String, archived: String = #","archived":true"#) -> String {
        """
        {"id":"\(uuidString(id))","name":"\(name)","kind":"expense","tax_line":null,"deductible_pct":100,
         "monthly_budget":"250.00"\(archived),"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}
        """
    }

    @Test func createCategoryPostsTheDraftWithItsIdAndOnlyTheSetFields() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let draft = CategoryDraft(name: "Pets", kind: .expense, monthlyBudget: Money(Decimal(string: "250.50")!))
        var requests = reply(201, body: Data(categoryJSON(id: draft.id, name: "Pets", archived: "").utf8))
            .makeAsyncIterator()

        let saved = try await api.createCategory(draft)
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "POST")
        #expect(request.url?.path() == "/api/categories")
        let body = try #require(bodyData(request))
        let json = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
        #expect(Set(json.keys) == ["id", "name", "kind", "monthly_budget"])
        #expect((json["id"] as? String)?.lowercased() == uuidString(draft.id))
        #expect(json["name"] as? String == "Pets")
        #expect(json["kind"] as? String == "expense")
        #expect(json["monthly_budget"] as? String == "250.50")
        #expect(saved.id == draft.id)
    }

    @Test func createCategoryWithoutABudgetOmitsIt() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let draft = CategoryDraft(name: "Refunds", kind: .income)
        var requests = reply(201, body: Data(categoryJSON(id: draft.id, name: "Refunds").utf8)).makeAsyncIterator()

        _ = try await api.createCategory(draft)
        let request = try #require(await requests.next())
        let body = try #require(bodyData(request))
        let json = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
        #expect(Set(json.keys) == ["id", "name", "kind"])
        #expect(json["kind"] as? String == "income")
    }

    @Test func updateCategoryPatchesTheIdWithOnlyTheSetKeys() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let id = UUID()
        var requests = reply(200, body: Data(categoryJSON(id: id, name: "Pets").utf8)).makeAsyncIterator()

        let saved = try await api.updateCategory(id: id, patch: CategoryPatch(monthlyBudget: .some(nil), archived: true))
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "PATCH")
        #expect(request.url?.path() == "/api/categories/\(uuidString(id))")
        let body = try #require(bodyData(request))
        let expected = ["monthly_budget": NSNull(), "archived": true] as NSDictionary
        #expect(try JSONSerialization.jsonObject(with: body) as? NSDictionary == expected)
        #expect(saved.archived)
    }

    @Test func categoriesDecodeArchivedAndDefaultItToFalseWhenTheServerOmitsIt() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let archived = UUID()
        let plain = UUID()
        let body = "[\(categoryJSON(id: archived, name: "Old"))," + categoryJSON(id: plain, name: "Dining", archived: "") + "]"
        _ = reply(200, body: Data(body.utf8))

        let categories = try await api.categories()
        #expect(categories.map(\.archived) == [true, false])
        #expect(categories.pickable.map(\.id) == [plain])
    }

    @Test func pickableKeepingTheSelectionKeepsThatArchivedCategoryAndDropsTheOthers() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let chosen = UUID()
        let other = UUID()
        let plain = UUID()
        let body = "[" + [
            categoryJSON(id: chosen, name: "Chosen"),
            categoryJSON(id: other, name: "Other"),
            categoryJSON(id: plain, name: "Dining", archived: "")
        ].joined(separator: ",") + "]"
        _ = reply(200, body: Data(body.utf8))

        let categories = try await api.categories()
        #expect(categories.pickable(keeping: chosen).map(\.id) == [chosen, plain])
        #expect(categories.pickable(keeping: nil).map(\.id) == [plain])
    }

    @Test func referenceDataKeepsArchivedCategoriesForNameLookup() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let archived = UUID()
        let plain = UUID()
        let categories = "[\(categoryJSON(id: archived, name: "Old")),\(categoryJSON(id: plain, name: "Dining", archived: ""))]"
        StubURLProtocol.respond { request in
            let body = request.url!.path() == "/api/categories" ? categories : "[]"
            return (StubURLProtocol.response(to: request, status: 200), Data(body.utf8))
        }
        let reference = ReferenceData()

        await reference.load(api: api)

        #expect(reference.categories.name(for: archived) == "Old")
        #expect(reference.categories.count == 2)
    }

    @Test func aDuplicateCategoryNameIsReportedInPlainWords() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let envelope = #"{"error":{"code":"conflict","message":"Conflicts with the current state."}}"#
        _ = reply(409, body: Data(envelope.utf8))
        let model = NewCategoryModel()

        let created = await model.create(CategoryDraft(name: "Pets", kind: .expense), api: api)
        #expect(created == nil)
        #expect(model.error?.status == 409)
        #expect(model.error?.localizedDescription == "You already have a category with that name")
    }

    @Test func otherCategoryCreateFailuresKeepTheServerMessage() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let envelope = #"{"error":{"code":"internal","message":"Something broke."}}"#
        _ = reply(500, body: Data(envelope.utf8))
        let model = NewCategoryModel()

        let created = await model.create(CategoryDraft(name: "Pets", kind: .expense), api: api)
        #expect(created == nil)
        #expect(model.error?.localizedDescription == "Something broke.")
    }

    // MARK: Add screen loading

    private func accountJSON(_ id: UUID, name: String) -> String {
        """
        {"id":"\(uuidString(id))","name":"\(name)","kind":"credit_card","institution":null,"simplefin_account_id":null,
         "last4":"1234","due_day":null,"planned_payment":null,"apr":null,"promo_apr_end":null,"active":true,
         "created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}
        """
    }

    private func categoriesJSON(_ names: [String]) -> String {
        let rows = names.map {
            """
            {"id":"\(UUID().uuidString.lowercased())","name":"\($0)","kind":"expense","tax_line":null,"deductible_pct":0,
             "monthly_budget":null,"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}
            """
        }
        return "[" + rows.joined(separator: ",") + "]"
    }

    /// Serves the Add screen's three lists; a path in `failing` answers 500.
    private func serveAddLists(accounts: [(UUID, String)], failing: Set<String> = []) {
        let accountsBody = "[" + accounts.map { accountJSON($0.0, name: $0.1) }.joined(separator: ",") + "]"
        let categories = categoriesJSON(["Groceries"])
        StubURLProtocol.respond { request in
            let path = request.url!.path()
            if failing.contains(path) {
                return (StubURLProtocol.response(to: request, status: 500), Data(#"{"error":{"code":"internal","message":"Down.","request_id":"r9"}}"#.utf8))
            }
            let body = switch path {
            case "/api/accounts": accountsBody
            case "/api/categories": categories
            default: "[]"
            }
            return (StubURLProtocol.response(to: request, status: 200), Data(body.utf8))
        }
    }

    @Test func addModelLoadKeepsTheOtherListsWhenOneFails() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let card = UUID()
        for failing in ["/api/accounts", "/api/categories", "/api/events"] {
            serveAddLists(accounts: [(card, "Chase")], failing: [failing])
            let model = AddModel(preferences: CapturePreferences(defaults: fixture.defaults))
            await model.load(api: api)
            #expect(model.hasLoaded)
            #expect(model.loadError == "Down.")
            #expect(model.accounts.isEmpty == (failing == "/api/accounts"))
            #expect(model.categories.isEmpty == (failing == "/api/categories"))
            #expect(model.accountId == (failing == "/api/accounts" ? nil : card))
        }
    }

    @Test func addModelLoadHasNoErrorWhenEverythingLoads() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        serveAddLists(accounts: [(UUID(), "Chase")])
        let model = AddModel(preferences: CapturePreferences(defaults: fixture.defaults))
        await model.load(api: api)
        #expect(model.loadError == nil)
        #expect(model.categories.map(\.name) == ["Groceries"])
    }

    @Test func addModelLoadKeepsACardPickedBeforeItFinishes() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let first = UUID()
        let second = UUID()
        serveAddLists(accounts: [(first, "First"), (second, "Second")])
        let model = AddModel(preferences: CapturePreferences(defaults: fixture.defaults))
        model.selectAccount(second)
        await model.load(api: api)
        #expect(model.accountId == second)
    }
}

@MainActor
struct SessionTests {
    private let server = URL(string: "http://10.0.0.5:8000")!
    private let fixture = TestSession()

    @Test func signInPersistsTokenAndServerURL() throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        #expect(!session.isSignedIn)

        try session.signIn(token: "abc", serverURL: server)
        #expect(session.accessToken == "abc")
        #expect(try Keychain.read(fixture.tokenKey) == "abc")

        let relaunched = fixture.make()
        #expect(relaunched.accessToken == "abc")
        #expect(relaunched.serverURL == server)
    }

    @Test func signOutClearsToken() throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "abc", serverURL: server)

        session.signOut()
        #expect(!session.isSignedIn)
        #expect(session.lastError == nil)
        #expect(try Keychain.read(fixture.tokenKey) == nil)

        let relaunched = fixture.make()
        #expect(!relaunched.isSignedIn)
        #expect(relaunched.serverURL == server)
    }

    @Test func rejectedTokenSignsOutOnlyWhenCurrent() throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "new", serverURL: server)

        session.tokenRejected("old")
        #expect(session.accessToken == "new")

        session.tokenRejected("new")
        #expect(!session.isSignedIn)
    }
}
