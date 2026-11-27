import Foundation
import os
import Testing
@testable import Tally

// MARK: - Network (shares StubURLProtocol, so these run in the serialized suite)

extension APIClientTests {
    @Test func chatSendsTheQuestionAndKeepsOneConversation() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let api = makeClient(session)
        let model = ChatModel()
        let firstId = model.conversationId
        let (requests, seen) = AsyncStream.makeStream(of: URLRequest.self)
        StubURLProtocol.respond { request in
            seen.yield(request)
            let body = #"{"conversation_id":"\#(firstId.uuidString.lowercased())","reply":"You spent **$40.00** on gas.","looked_up":["Spending by category, 2026-09"]}"#
            return (StubURLProtocol.response(to: request, status: 200), Data(body.utf8))
        }

        model.draft = "  Gas in September?  "
        await model.send(api: api)
        model.draft = "And August?"
        await model.send(api: api)

        #expect(model.lines.map(\.role) == [.you, .tally, .you, .tally])
        #expect(model.lines[0].text == "Gas in September?")
        #expect(model.lines[1].lookedUp == ["Spending by category, 2026-09"])
        var iterator = requests.makeAsyncIterator()
        for _ in 0..<2 {
            let request = try #require(await iterator.next())
            #expect(request.url?.path() == "/api/ai/chat")
            let body = try #require(bodyData(request))
            let json = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
            #expect(json["conversation_id"] as? String == firstId.uuidString)
            #expect(json["actions_supported"] as? Bool == true)
        }

        model.startOver()
        #expect(model.lines.isEmpty)
        #expect(model.conversationId != firstId)
    }

    @Test func aFailedQuestionGoesBackInTheField() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let api = makeClient(session)
        let envelope = #"{"error":{"code":"service_unavailable","message":"AI assist has reached this month's budget.","request_id":"r1"}}"#
        _ = reply(503, body: Data(envelope.utf8))
        let model = ChatModel()
        model.draft = "Gas?"

        await model.send(api: api)

        #expect(model.lines.map(\.text) == ["Gas?"])
        #expect(model.lines[0].failure == "AI assist has reached this month's budget.")
        #expect(model.draft.isEmpty)
        #expect(!model.isSending)

        let body = #"{"conversation_id":"\#(model.conversationId.uuidString.lowercased())","reply":"$40.","looked_up":[]}"#
        _ = reply(200, body: Data(body.utf8))
        await model.retry(api: api)

        #expect(model.lines.map(\.text) == ["Gas?", "$40."])
        #expect(model.lines.allSatisfy { $0.failure == nil })
    }

    @Test func aStarterPromptSendsItsText() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let model = ChatModel()
        let body = #"{"conversation_id":"\#(model.conversationId.uuidString.lowercased())","reply":"Groceries.","looked_up":[]}"#
        var requests = reply(200, body: Data(body.utf8)).makeAsyncIterator()

        await model.sendStarter(ChatModel.starterPrompts[0], api: makeClient(session))

        #expect(model.lines.map(\.text) == [ChatModel.starterPrompts[0], "Groceries."])
        let request = try #require(await requests.next())
        let data = try #require(bodyData(request))
        let json = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
        #expect(json["message"] as? String == ChatModel.starterPrompts[0])
    }

    @Test func theOpeningQuestionIsSentOnce() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let model = ChatModel()
        let count = OSAllocatedUnfairLock(initialState: 0)
        let body = #"{"conversation_id":"\#(model.conversationId.uuidString.lowercased())","reply":"Ok.","looked_up":[]}"#
        StubURLProtocol.respond { request in
            count.withLock { $0 += 1 }
            return (StubURLProtocol.response(to: request, status: 200), Data(body.utf8))
        }
        let api = makeClient(session)

        await model.start(with: "Where can I cut back this month?", api: api)
        await model.start(with: "Where can I cut back this month?", api: api)

        #expect(count.withLock { $0 } == 1)
        #expect(model.lines.count == 2)
    }

    @Test func aCancelledOpeningQuestionStaysWithRetryAndIsNotResent() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let model = ChatModel()
        let count = OSAllocatedUnfairLock(initialState: 0)
        let body = #"{"conversation_id":"\#(model.conversationId.uuidString.lowercased())","reply":"Ok.","looked_up":[]}"#
        StubURLProtocol.respond { request in
            count.withLock { $0 += 1 }
            return (StubURLProtocol.response(to: request, status: 200), Data(body.utf8))
        }
        let api = makeClient(session)

        let task = Task { await model.start(with: "Cut back?", api: api) }
        task.cancel()
        await task.value
        let sentBefore = count.withLock { $0 }
        await model.start(with: "Cut back?", api: api)

        #expect(model.lines.map(\.text) == ["Cut back?"])
        #expect(model.lines.first?.failure != nil)
        #expect(count.withLock { $0 } == sentBefore)

        await model.retry(api: api)
        #expect(model.lines.map(\.text) == ["Cut back?", "Ok."])
    }
}

// MARK: - Rendering

struct ChatMarkdownTests {
    @Test func linksInAReplyRenderAsPlainTextButEmphasisStays() {
        let text = ChatView.markdown("You spent **$40.00**, see [details](https://evil.example/?d=40) or <https://evil.example>.")

        #expect(text.runs.allSatisfy { $0.link == nil })
        #expect(String(text.characters) == "You spent $40.00, see details or https://evil.example.")
        #expect(text.runs.contains { $0.inlinePresentationIntent == .stronglyEmphasized })
    }
}

// MARK: - AI actions (#223)

extension APIClientTests {
    private func actionJSON(status: String = "applied") throws -> Data {
        let json = try #require(String(data: jsonFixture("ai-action"), encoding: .utf8))
        return Data(json.replacingOccurrences(of: "\"status\":\"applied\"", with: "\"status\":\"\(status)\"").utf8)
    }

    @Test func anActionDecodesEveryStepKindAndItsEffects() throws {
        let action = try JSONDecoder.api.decode(AIAction.self, from: jsonFixture("ai-action"))

        #expect(action.status == .applied)
        #expect(action.source == .chat)
        #expect(action.steps.map(\.kind) == [
            .createCategory, .createRule, .createSubscription, .updateSubscription, .categorizeTransactions, .markReviewed,
        ])
        #expect(action.steps.map(\.name) == ["Pets", "PETCO", "Netflix", "Car Loan", nil, nil])
        #expect(action.steps.map(\.transactionCount) == [0, 0, 0, 0, 2, 2])
        #expect(action.effects == AIActionEffects(
            transactions: 3, categoriesCreated: 1, rulesCreated: 1, subscriptionsCreated: 1, subscriptionsUpdated: 1))
        #expect(action.appliedAt != nil)
        #expect(action.error == nil)
    }

    @Test func anActionWithoutAppliedAtOrErrorKeysDecodes() throws {
        let json = #"{"id":"5b0f1c0e-6c1e-4e0a-9d52-0d1f3a7a0001","summary":"s","status":"proposed","source":"insight","steps":[{"kind":"mark_reviewed","ids":["5b0f1c0e-6c1e-4e0a-9d52-0d1f3a7a0011"]}],"created_at":"2026-10-20T15:04:05Z","effects":{"transactions":1,"categories_created":0,"rules_created":0,"subscriptions_created":0,"subscriptions_updated":0}}"#
        let action = try JSONDecoder.api.decode(AIAction.self, from: Data(json.utf8))
        #expect(action.status == .proposed)
        #expect(action.source == .insight)
        #expect(action.appliedAt == nil)
        #expect(action.error == nil)
    }

    @Test func unknownStatusSourceAndStepKindsStillDecode() throws {
        let json = #"{"id":"5b0f1c0e-6c1e-4e0a-9d52-0d1f3a7a0001","summary":"s","status":"paused","source":"email","steps":[{"kind":"teleport"}],"created_at":"2026-10-20T15:04:05Z","effects":{"transactions":0,"categories_created":0,"rules_created":0,"subscriptions_created":0,"subscriptions_updated":0}}"#
        let action = try JSONDecoder.api.decode(AIAction.self, from: Data(json.utf8))
        #expect(action.status == .other("paused"))
        #expect(action.source == .other("email"))
        #expect(action.steps.first?.kind == .other("teleport"))
    }

    @Test func aChatReplyDecodesWithAndWithoutActions() throws {
        let old = #"{"conversation_id":"5b0f1c0e-6c1e-4e0a-9d52-0d1f3a7a0001","reply":"Hi","looked_up":[]}"#
        #expect(try JSONDecoder.api.decode(ChatReply.self, from: Data(old.utf8)).actions.isEmpty)

        let action = try #require(String(data: jsonFixture("ai-action"), encoding: .utf8))
        let new = #"{"conversation_id":"5b0f1c0e-6c1e-4e0a-9d52-0d1f3a7a0001","reply":"Hi","looked_up":[],"actions":[\#(action)]}"#
        let reply = try JSONDecoder.api.decode(ChatReply.self, from: Data(new.utf8))
        #expect(reply.actions.count == 1)
        #expect(reply.actions.first?.steps.count == 6)
    }

    @Test func actionEndpointsUseTheRightMethodAndPath() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let api = makeClient(session)
        let id = UUID()
        let path = "/api/ai/actions/\(id.uuidString.lowercased())"
        let single = try actionJSON()
        var requests = reply(200, body: Data("[\(String(decoding: single, as: UTF8.self))]".utf8)).makeAsyncIterator()

        #expect(try await api.aiActions().count == 1)
        var request = try #require(await requests.next())
        #expect(request.httpMethod == "GET")
        #expect(request.url?.path() == "/api/ai/actions")
        #expect(request.url?.query() == nil)

        _ = try await api.aiActions(status: .proposed)
        request = try #require(await requests.next())
        #expect(request.url?.query() == "status=proposed")

        requests = reply(200, body: single).makeAsyncIterator()
        #expect(try await api.aiAction(id: id).status == .applied)
        request = try #require(await requests.next())
        #expect(request.httpMethod == "GET")
        #expect(request.url?.path() == path)

        for (suffix, call) in [
            ("confirm", api.confirmAIAction), ("dismiss", api.dismissAIAction), ("undo", api.undoAIAction),
        ] {
            _ = try await call(id)
            request = try #require(await requests.next())
            #expect(request.httpMethod == "POST")
            #expect(request.url?.path() == "\(path)/\(suffix)")
        }
    }

    @Test func aRefusedActionSurfacesTheServersMessage() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let api = makeClient(session)
        let message = "A transaction changed since this was applied."
        _ = reply(409, body: Data(#"{"error":{"code":"conflict","message":"\#(message)","request_id":"r1"}}"#.utf8))

        do {
            _ = try await api.undoAIAction(id: UUID())
            Issue.record("expected a 409")
        } catch let error as APIError {
            #expect(error.localizedDescription == message)
            guard case .server(let serverError) = error else {
                Issue.record("not a server error")
                return
            }
            #expect(serverError.status == 409)
        }
    }
}

// MARK: - Action cards (#224)

extension APIClientTests {
    private func proposedModel(_ api: APIClient) async throws -> (ChatModel, AIAction) {
        let proposed = try #require(String(data: jsonFixture("ai-action"), encoding: .utf8))
            .replacingOccurrences(of: "\"status\":\"applied\"", with: "\"status\":\"proposed\"")
        let model = ChatModel()
        let body = #"{"conversation_id":"\#(model.conversationId.uuidString.lowercased())","reply":"Done?","looked_up":[],"actions":[\#(proposed)]}"#
        _ = reply(200, body: Data(body.utf8))
        model.draft = "Tidy up"
        await model.send(api: api)
        return (model, try #require(model.lines.last?.actions.first))
    }

    private func actionBody(_ status: String) throws -> Data {
        let json = try #require(String(data: jsonFixture("ai-action"), encoding: .utf8))
        return Data(json.replacingOccurrences(of: "\"status\":\"applied\"", with: "\"status\":\"\(status)\"").utf8)
    }

    @Test func confirmingAppliesTheCardAndUndoingUndoesIt() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let api = makeClient(session)
        let (model, action) = try await proposedModel(api)
        #expect(action.status == .proposed)
        var edits = 0

        _ = reply(200, body: try actionBody("applied"))
        await model.confirm(action, api: api) { edits += 1 }
        #expect(model.lines.last?.actions.first?.status == .applied)
        #expect(edits == 1)

        _ = reply(200, body: try actionBody("undone"))
        await model.undo(action, api: api) { edits += 1 }
        #expect(model.lines.last?.actions.first?.status == .undone)
        #expect(edits == 2)
        #expect(model.busyActions.isEmpty)
    }

    @Test func aCategorizeStepNamesItsCategoryFromTheList() throws {
        let action = try JSONDecoder.api.decode(AIAction.self, from: jsonFixture("ai-action"))
        let step = try #require(action.steps.first { $0.kind == .categorizeTransactions })
        #expect(step.categoryId == UUID(uuidString: "5b0f1c0e-6c1e-4e0a-9d52-0d1f3a7a0002"))
        #expect(ActionCard.label(for: step, categories: []) == "Categorize 2 transactions")
    }

    @Test func notNowDismissesTheCardWithoutRefreshing() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let api = makeClient(session)
        let (model, action) = try await proposedModel(api)

        _ = reply(200, body: try actionBody("dismissed"))
        await model.dismiss(action, api: api)

        #expect(model.lines.last?.actions.first?.status == .dismissed)
    }

    @Test func aRefusedConfirmShowsTheServersMessageAndStaysProposed() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let api = makeClient(session)
        let (model, action) = try await proposedModel(api)
        let message = "A transaction changed since this was proposed."
        _ = reply(409, body: Data(#"{"error":{"code":"conflict","message":"\#(message)","request_id":"r1"}}"#.utf8))
        var edits = 0

        await model.confirm(action, api: api) { edits += 1 }

        #expect(model.actionNotices[action.id] == message)
        #expect(model.lines.last?.actions.first?.status == .proposed)
        #expect(edits == 0)
        #expect(model.busyActions.isEmpty)
    }
}

extension APIClientTests {
    @Test func aConflictResyncsTheCardWithTheServer() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let api = makeClient(session)
        let (model, action) = try await proposedModel(api)
        let applied = try actionBody("applied")
        let message = "Already applied."
        StubURLProtocol.respond { request in
            if request.httpMethod == "GET" {
                return (StubURLProtocol.response(to: request, status: 200), applied)
            }
            let body = #"{"error":{"code":"conflict","message":"\#(message)","request_id":"r1"}}"#
            return (StubURLProtocol.response(to: request, status: 409), Data(body.utf8))
        }

        await model.confirm(action, api: api)

        #expect(model.lines.last?.actions.first?.status == .applied)
        #expect(model.actionNotices[action.id] == message)
    }

    @Test func newChatWaitsForAnActionInFlight() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let api = makeClient(session)
        let (model, action) = try await proposedModel(api)
        #expect(model.canStartOver)
        _ = reply(200, body: try actionBody("applied"))

        let task = Task { await model.confirm(action, api: api) }
        while model.busyActions.isEmpty && !task.isCancelled { await Task.yield() }
        #expect(!model.canStartOver)
        model.startOver()
        #expect(!model.lines.isEmpty)
        await task.value

        #expect(model.canStartOver)
    }
}

@MainActor struct ActionCardTests {
    @Test func noCountedEffectsMeansNothingToConfirm() {
        let none = AIActionEffects(
            transactions: 0, categoriesCreated: 0, rulesCreated: 0, subscriptionsCreated: 0, subscriptionsUpdated: 0)
        #expect(none.lines().isEmpty)
    }

    @Test func effectsLinesCountOnlyWhatChanges() {
        let mixed = AIActionEffects(
            transactions: 3, categoriesCreated: 1, categoriesUpdated: 2, rulesCreated: 2, subscriptionsCreated: 1, subscriptionsUpdated: 4)
        #expect(mixed.lines() == [
            "Updates 3 transactions", "Creates 1 category", "Updates 2 categories", "Creates 2 rules", "Adds 1 subscription", "Changes 4 subscriptions",
        ])
        let one = AIActionEffects(
            transactions: 1, categoriesCreated: 0, rulesCreated: 0, subscriptionsCreated: 0, subscriptionsUpdated: 0)
        #expect(one.lines() == ["Updates 1 transaction"])
    }

    private static let groceries = "5b0f1c0e-6c1e-4e0a-9d52-0d1f3a7a0020"
    private static let gas = "5b0f1c0e-6c1e-4e0a-9d52-0d1f3a7a0021"

    private func action(steps: String, categoriesUpdated: Int) throws -> AIAction {
        let json = #"{"id":"5b0f1c0e-6c1e-4e0a-9d52-0d1f3a7a0001","summary":"s","status":"proposed","source":"chat","steps":["#
            + steps
            + #"],"created_at":"2026-10-20T15:04:05Z","effects":{"transactions":0,"categories_created":0,"categories_updated":"#
            + "\(categoriesUpdated)"
            + #","rules_created":0,"subscriptions_created":0,"subscriptions_updated":0}}"#
        return try JSONDecoder.api.decode(AIAction.self, from: Data(json.utf8))
    }

    private func category(_ id: String, _ name: String) throws -> Tally.Category {
        let json = #"{"id":""# + id + #"","name":""# + name
            + #"","kind":"expense","tax_line":null,"deductible_pct":100,"monthly_budget":null,"archived":false,"created_at":"2026-10-01T00:00:00Z","updated_at":"2026-10-01T00:00:00Z"}"#
        return try JSONDecoder.api.decode(Tally.Category.self, from: Data(json.utf8))
    }

    @Test func budgetOnlyCategoryUpdatesReadAsBudgetChanges() throws {
        let budgets = try action(
            steps: #"{"kind":"update_category","id":""# + Self.groceries + #"","patch":{"monthly_budget":"450.00"}},"#
                + #"{"kind":"update_category","id":""# + Self.gas + #"","patch":{"monthly_budget":null}}"#,
            categoriesUpdated: 2)
        #expect(budgets.steps.map(\.kind) == [.updateCategory, .updateCategory])
        #expect(budgets.steps[0].id == UUID(uuidString: Self.groceries))
        #expect(budgets.steps[0].monthlyBudget == Money(450))
        #expect(budgets.steps[1].patchFields == ["monthlyBudget"] && budgets.steps[1].monthlyBudget == nil)
        #expect(budgets.effects.categoriesUpdated == 2)
        #expect(budgets.lines == ["Changes 2 budgets"])
        let categories = [try category(Self.groceries, "Groceries"), try category(Self.gas, "Gas")]
        #expect(budgets.steps.map { ActionCard.label(for: $0, categories: categories) } == [
            "Set Groceries budget to $450.00", "Clear Gas budget",
        ])
    }

    @Test func aCategoryCreatedEarlierInTheActionKeepsItsName() throws {
        let created = try action(
            steps: #"{"kind":"create_category","category":{"id":""# + Self.gas + #"","name":"Fuel","kind":"expense"}},"#
                + #"{"kind":"update_category","id":""# + Self.gas + #"","patch":{"monthly_budget":"80.00"}}"#,
            categoriesUpdated: 0)
        #expect(created.steps[0].createdCategoryId == UUID(uuidString: Self.gas))
        #expect(ActionCard.label(for: created.steps[1], categories: [], steps: created.steps) == "Set Fuel budget to $80.00")
        #expect(ActionCard.label(for: created.steps[1], categories: []) == "Set a category budget to $80.00")
    }

    @Test func anyOtherCategoryChangeReadsAsCategoryUpdates() throws {
        let mixed = try action(
            steps: #"{"kind":"update_category","id":""# + Self.groceries + #"","patch":{"monthly_budget":"450.00"}},"#
                + #"{"kind":"update_category","id":""# + Self.gas + #"","patch":{"archived":true}}"#,
            categoriesUpdated: 2)
        #expect(mixed.steps[1].archived == true)
        #expect(mixed.lines == ["Updates 2 categories"])
        let categories = [try category(Self.gas, "Gas")]
        #expect(ActionCard.label(for: mixed.steps[1], categories: categories) == "Change category Gas: archive")
        let renamed = try action(
            steps: #"{"kind":"update_category","id":""# + Self.gas + #"","patch":{"name":"Fuel","monthly_budget":"80.00"}}"#,
            categoriesUpdated: 1)
        #expect(renamed.lines == ["Updates 1 category"])
        #expect(ActionCard.label(for: renamed.steps[0], categories: categories)
            == "Change category Gas: rename to Fuel, budget $80.00")
    }
}

// MARK: - Timeouts and waiting actions

extension APIClientTests {
    @Test func chatAllowsThreeMinutesAndOtherRequestsKeepTheDefault() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let api = makeClient(session)
        let conversation = UUID()
        let body = #"{"conversation_id":"\#(conversation.uuidString.lowercased())","reply":"Hi","looked_up":[]}"#
        var requests = reply(200, body: Data(body.utf8)).makeAsyncIterator()

        _ = try await api.chat(conversationId: conversation, message: "Hi")
        let chat = try #require(await requests.next())
        #expect(chat.timeoutInterval == 180)

        requests = reply(200, body: Data("[]".utf8)).makeAsyncIterator()
        _ = try await api.aiActions()
        let plain = try #require(await requests.next())
        #expect(plain.timeoutInterval == URLRequest(url: server).timeoutInterval)
    }

    private func proposedJSON() throws -> String {
        try #require(String(data: jsonFixture("ai-action"), encoding: .utf8))
            .replacingOccurrences(of: "\"status\":\"applied\"", with: "\"status\":\"proposed\"")
    }

    @Test func pendingChatActionsFillTheWaitingSectionAndSkipInsights() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let api = makeClient(session)
        let chat = try proposedJSON()
        let insight = chat.replacingOccurrences(of: "\"source\":\"chat\"", with: "\"source\":\"insight\"")
            .replacingOccurrences(of: "5b0f1c0e-6c1e-4e0a-9d52-0d1f3a7a0001", with: "5b0f1c0e-6c1e-4e0a-9d52-0d1f3a7a00ff")
        _ = reply(200, body: Data("[\(chat),\(insight)]".utf8))
        let model = ChatModel()

        await model.loadPending(api: api)

        #expect(model.waitingActions.count == 1)
        #expect(model.waitingActions.first?.source == .chat)

        let action = try #require(model.waitingActions.first)
        _ = reply(200, body: try actionBodyForWaiting("applied"))
        await model.confirm(action, api: api)
        #expect(model.waitingActions.first?.status == .applied)
    }

    private func actionBodyForWaiting(_ status: String) throws -> Data {
        Data(try proposedJSON().replacingOccurrences(of: "\"status\":\"proposed\"", with: "\"status\":\"\(status)\"").utf8)
    }

    @Test func anActionShownUnderAnAnswerIsNotRepeatedWaiting() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let api = makeClient(session)
        let proposed = try proposedJSON()
        let model = ChatModel()
        let answer = #"{"conversation_id":"\#(model.conversationId.uuidString.lowercased())","reply":"Done?","looked_up":[],"actions":[\#(proposed)]}"#
        _ = reply(200, body: Data(answer.utf8))
        model.draft = "Tidy up"
        await model.send(api: api)

        _ = reply(200, body: Data("[\(proposed)]".utf8))
        await model.loadPending(api: api)

        #expect(model.pendingActions.count == 1)
        #expect(model.waitingActions.isEmpty)
    }

    @Test func aTimedOutSendReloadsWaitingActionsAndExplainsTheWait() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let api = makeClient(session)
        let proposed = try proposedJSON()
        StubURLProtocol.respond { request in
            if request.httpMethod == "GET" {
                return (StubURLProtocol.response(to: request, status: 200), Data("[\(proposed)]".utf8))
            }
            throw URLError(.timedOut)
        }
        let model = ChatModel()
        model.draft = "Tidy up"

        await model.send(api: api)

        #expect(model.lines.first?.failure == "This is taking longer than usual. If Tally suggested changes, they'll appear under Waiting for you.")
        #expect(model.waitingActions.count == 1)
    }
}
