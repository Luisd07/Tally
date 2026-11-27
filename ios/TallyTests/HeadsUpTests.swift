import Foundation
import os
import Testing
@testable import Tally

// MARK: - Network (shares StubURLProtocol, so these run in the serialized suite)

extension APIClientTests {
    private static let insightId = "7c1d0a52-0000-4000-8000-000000000001"
    private static let actionId = "5b0f1c0e-6c1e-4e0a-9d52-0d1f3a7a0001"

    private func insightJSON(
        id: String = insightId, kind: String = "unusual_charge", transaction: String? = nil,
        action: String? = nil, actionStatus: String? = nil
    ) -> String {
        """
        {"id":"\(id)","kind":"\(kind)","title":"Big charge","message":"Costco was $212, about 4x usual.",
         "day":"2026-10-25","category_id":null,"transaction_id":\(transaction.map { "\"\($0)\"" } ?? "null"),
         "subscription_id":null,"action_id":\(action.map { "\"\($0)\"" } ?? "null"),
         "action_status":\(actionStatus.map { "\"\($0)\"" } ?? "null"),
         "dismissed_at":null,"created_at":"2026-10-26T06:00:00Z"}
        """
    }

    /// Answers by method and path, records them, and 200s with `{}`-free bodies from `routes`.
    private func route(_ routes: [String: String]) -> OSAllocatedUnfairLock<[String]> {
        let seen = OSAllocatedUnfairLock<[String]>(initialState: [])
        StubURLProtocol.respond { request in
            let key = "\(request.httpMethod ?? "GET") \(request.url?.path() ?? "")"
            seen.withLock { $0.append(key) }
            guard let body = routes[key] else { return (StubURLProtocol.response(to: request, status: 404), Data()) }
            return (StubURLProtocol.response(to: request, status: 200), Data(body.utf8))
        }
        return seen
    }

    private func proposedAction() throws -> String {
        let json = try #require(String(data: jsonFixture("ai-action"), encoding: .utf8))
        return json.replacingOccurrences(of: "\"status\":\"applied\"", with: "\"status\":\"proposed\"")
    }

    private func loadedModel(
        insight: String, routes: [String: String]
    ) async throws -> (HeadsUpModel, APIClient, OSAllocatedUnfairLock<[String]>) {
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let seen = route(routes.merging(["GET /api/insights": "[\(insight)]"]) { $1 })
        let api = makeClient(session)
        let model = HeadsUpModel()
        await model.load(api: api)
        return (model, api, seen)
    }

    @Test func insightsDecodeARealPayload() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let transaction = "9a1d0a52-0000-4000-8000-000000000009"
        let body = "[\(insightJSON(transaction: transaction, action: Self.actionId, actionStatus: "proposed")),"
            + "\(insightJSON(id: "7c1d0a52-0000-4000-8000-000000000002", kind: "weird_new_kind"))]"
        let seen = route(["GET /api/insights": body])

        let insights = try await makeClient(session).insights()

        #expect(insights.count == 2)
        #expect(insights[0].kind == .unusualCharge)
        #expect(insights[0].title == "Big charge")
        #expect(insights[0].day == CalendarDate(year: 2026, month: 10, day: 25))
        #expect(insights[0].transactionId == UUID(uuidString: transaction))
        #expect(insights[0].actionId == UUID(uuidString: Self.actionId))
        #expect(insights[0].actionStatus == .proposed)
        #expect(insights[1].kind == .other("weird_new_kind"))
        #expect(insights[1].actionId == nil)
        #expect(seen.withLock { $0 } == ["GET /api/insights"])
    }

    @Test func dismissRemovesTheRowAndCallsTheEndpoint() async throws {
        defer { try? fixture.cleanUp() }
        let insight = insightJSON()
        let (model, api, seen) = try await loadedModel(
            insight: insight, routes: ["POST /api/insights/\(Self.insightId)/dismiss": insight]
        )
        #expect(model.insights.count == 1)

        await model.dismiss(model.insights[0], api: api)

        #expect(model.insights.isEmpty)
        #expect(seen.withLock { $0 }.contains("POST /api/insights/\(Self.insightId)/dismiss"))
    }

    @Test func aFailedDismissKeepsTheRowAndSaysWhy() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api, _) = try await loadedModel(insight: insightJSON(), routes: [:])

        await model.dismiss(model.insights[0], api: api)

        #expect(model.insights.count == 1)
        #expect(model.failures[model.insights[0].id] != nil)
    }

    @Test func aFailedLoadShowsNothing() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        _ = route([:])
        let model = HeadsUpModel()

        await model.load(api: makeClient(session))

        #expect(model.isEmpty)
    }

    @Test func confirmApplesTheActionAndMarksTheInsightDone() async throws {
        defer { try? fixture.cleanUp() }
        let applied = try jsonFixture("ai-action")
        let (model, api, seen) = try await loadedModel(
            insight: insightJSON(action: Self.actionId, actionStatus: "proposed"),
            routes: [
                "GET /api/ai/actions/\(Self.actionId)": try proposedAction(),
                "POST /api/ai/actions/\(Self.actionId)/confirm": String(decoding: applied, as: UTF8.self),
                "POST /api/insights/\(Self.insightId)/dismiss": insightJSON(),
            ]
        )
        let insight = model.insights[0]
        #expect(model.canConfirm(insight))
        #expect(model.actions[insight.id]?.lines.first == "Updates 3 transactions")

        await model.confirm(insight, api: api)

        #expect(model.done[insight.id]?.status == .applied)
        #expect(model.insights.count == 1)
        #expect(seen.withLock { $0 }.contains("POST /api/ai/actions/\(Self.actionId)/confirm"))

        await model.settle(insight, api: api)

        #expect(model.insights.isEmpty)
        #expect(seen.withLock { $0 }.last == "POST /api/insights/\(Self.insightId)/dismiss")
    }

    @Test func notNowDismissesTheActionAndTheInsight() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api, seen) = try await loadedModel(
            insight: insightJSON(action: Self.actionId, actionStatus: "proposed"),
            routes: [
                "GET /api/ai/actions/\(Self.actionId)": try proposedAction(),
                "POST /api/ai/actions/\(Self.actionId)/dismiss": try proposedAction(),
                "POST /api/insights/\(Self.insightId)/dismiss": insightJSON(),
            ]
        )

        await model.notNow(model.insights[0], api: api)

        #expect(model.insights.isEmpty)
        let calls = seen.withLock { $0 }
        #expect(calls.contains("POST /api/ai/actions/\(Self.actionId)/dismiss"))
        #expect(calls.contains("POST /api/insights/\(Self.insightId)/dismiss"))
    }

    @Test func undoRevertsTheActionAndDismissesTheInsight() async throws {
        defer { try? fixture.cleanUp() }
        let applied = String(decoding: try jsonFixture("ai-action"), as: UTF8.self)
        let (model, api, seen) = try await loadedModel(
            insight: insightJSON(action: Self.actionId, actionStatus: "proposed"),
            routes: [
                "GET /api/ai/actions/\(Self.actionId)": try proposedAction(),
                "POST /api/ai/actions/\(Self.actionId)/confirm": applied,
                "POST /api/ai/actions/\(Self.actionId)/undo": applied,
                "POST /api/insights/\(Self.insightId)/dismiss": insightJSON(),
            ]
        )
        let insight = model.insights[0]
        await model.confirm(insight, api: api)

        await model.undo(insight, api: api)

        #expect(model.insights.isEmpty)
        #expect(seen.withLock { $0 }.contains("POST /api/ai/actions/\(Self.actionId)/undo"))
    }

    @Test func anInsightWhoseActionAlreadyRanStaysOffTheCard() async throws {
        defer { try? fixture.cleanUp() }
        let (model, _, _) = try await loadedModel(
            insight: insightJSON(action: Self.actionId, actionStatus: "applied"), routes: [:]
        )
        #expect(model.isEmpty)
    }
}
