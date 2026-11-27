import Foundation
import os
import Testing
@testable import Tally

// MARK: - Network (shares StubURLProtocol, so these run in the serialized suite)

extension APIClientTests {
    private func taxReviewJSON(status: String, summary: String? = nil, items: String = "[]") -> String {
        let summaryJSON = summary.map { "\"\($0)\"" } ?? "null"
        return #"{"year":2026,"status":"\#(status)","summary":\#(summaryJSON),"error":null,"updated_at":"2027-01-10T15:00:00Z","items":\#(items)}"#
    }

    @Test func followingAReviewPollsUntilItsReady() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let id = UUID()
        let items = #"[{"kind":"missing_receipt","note":"No receipt.","transaction":{"id":"\#(id.uuidString.lowercased())","posted_date":"2026-03-04","description_raw":"DELTA","merchant":"Delta","amount":"-412.30"}},{"kind":"mileage_gap","note":"No miles in April.","transaction":null}]"#
        let replies = OSAllocatedUnfairLock(initialState: [
            taxReviewJSON(status: "requested"), taxReviewJSON(status: "running"),
            taxReviewJSON(status: "ready", summary: "Mostly travel.", items: items),
        ])
        let count = OSAllocatedUnfairLock(initialState: 0)
        StubURLProtocol.respond { request in
            count.withLock { $0 += 1 }
            let body = replies.withLock { $0.count > 1 ? $0.removeFirst() : $0[0] }
            return (StubURLProtocol.response(to: request, status: 200), Data(body.utf8))
        }
        let model = TaxReviewModel(pollInterval: .milliseconds(1))

        await model.follow(year: 2026, api: makeClient(session))

        let review = try #require(model.review)
        #expect(review.status == .ready)
        #expect(review.summary == "Mostly travel.")
        #expect(review.items.map(\.kind) == [.missingReceipt, .mileageGap])
        #expect(review.items[0].transaction?.id == id)
        #expect(review.items[1].transaction == nil)
        #expect(count.withLock { $0 } == 3)
    }

    @Test func noReviewYetStopsFollowing() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let requests = reply(200, body: Data("null".utf8))
        let model = TaxReviewModel(pollInterval: .milliseconds(1))

        await model.follow(year: 2026, api: makeClient(session))

        #expect(model.review == nil)
        var iterator = requests.makeAsyncIterator()
        let request = try #require(await iterator.next())
        #expect(request.url?.path() == "/api/ai/tax-review")
        #expect(request.url?.query() == "year=2026")
    }

    @Test func requestingAReviewThatsAlreadyRunningShowsWhy() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let envelope = #"{"error":{"code":"conflict","message":"A review of this year is already being written.","request_id":"r1"}}"#
        _ = reply(409, body: Data(envelope.utf8))
        let model = TaxReviewModel()

        await model.request(year: 2026, api: makeClient(session))

        #expect(model.error == "A review of this year is already being written.")
        #expect(!model.isRequesting)
    }

    @Test func aRefusedRequestKeepsItsErrorAndDoesntRestartFollowing() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let envelope = #"{"error":{"code":"service_unavailable","message":"AI assist is off.","request_id":"r1"}}"#
        _ = reply(503, body: Data(envelope.utf8))
        let model = TaxReviewModel()

        let accepted = await model.request(year: 2026, api: makeClient(session))

        #expect(!accepted)
        #expect(model.error == "AI assist is off.")
    }

    @Test func anAcceptedRequestRestartsFollowing() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        _ = reply(202, body: Data(taxReviewJSON(status: "requested").utf8))
        let model = TaxReviewModel()

        let accepted = await model.request(year: 2026, api: makeClient(session))

        #expect(accepted)
        #expect(model.error == nil)
        #expect(model.review?.status == .requested)
    }
}
