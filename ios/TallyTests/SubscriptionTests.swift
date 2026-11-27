import Foundation
import os
import Testing
@testable import Tally

/// Lives in `APIClientTests`' serialized suite because the network tests share `StubURLProtocol`'s one handler.
extension APIClientTests {
    private func subscriptionJSON(
        id: UUID,
        name: String = "Netflix",
        amount: String = "-15.49",
        cadence: String = "monthly",
        kind: String = "subscription",
        nextDue: String? = "2026-11-05",
        active: Bool = true,
        matchText: String? = nil
    ) -> String {
        let quote: (String?) -> String = { $0.map { "\"\($0)\"" } ?? "null" }
        return """
        {"id":"\(uuidString(id))","name":"\(name)","amount":"\(amount)","cadence":"\(cadence)","kind":"\(kind)",\
        "account_id":null,"category_id":null,"bucket":"personal","next_due":\(quote(nextDue)),"active":\(active),\
        "start_date":null,"end_date":null,"business_pct":null,"match_text":\(quote(matchText)),\
        "created_at":"2026-10-01T00:00:00Z","updated_at":"2026-10-01T00:00:00Z",\
        "flags":{"price_change":null,"missed":false,"dormant":false}}
        """
    }

    private func subscriptionPageJSON(_ rows: [String], subscription: String = "-15.49", bill: String = "0.00") -> Data {
        let annual = { (monthly: String) in monthly == "0.00" ? "0.00" : "\(Decimal(string: monthly)! * 12)" }
        return Data("""
        {"items":[\(rows.joined(separator: ","))],"totals":{\
        "subscription":{"monthly":"\(subscription)","annual":"\(annual(subscription))"},\
        "bill":{"monthly":"\(bill)","annual":"\(annual(bill))"}}}
        """.utf8)
    }

    private func requestJSON(_ request: URLRequest) throws -> [String: Any] {
        let body = try #require(bodyData(request))
        return try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
    }

    private func decodedSubscription(_ json: String) throws -> Subscription {
        try JSONDecoder.api.decode(Subscription.self, from: Data(json.utf8))
    }

    private func loadedSubscriptionsModel(
        _ rows: [String], includeInactive: Bool = false, subscription: String = "-15.49", bill: String = "0.00"
    ) async -> (SubscriptionsModel, APIClient) {
        let api = makeClient(fixture.make())
        let model = SubscriptionsModel()
        _ = reply(200, body: subscriptionPageJSON(rows, subscription: subscription, bill: bill))
        if includeInactive {
            await model.setIncludeInactive(true, api: api)
        } else {
            await model.load(api: api)
        }
        return (model, api)
    }

    // MARK: Models

    @Test func subscriptionFixtureDecodesWithTotalsPerKind() throws {
        let page = try JSONDecoder.api.decode(SubscriptionPage.self, from: jsonFixture("subscriptions"))
        #expect(page.items.count == 1)
        #expect(page.items[0].name == "Car Payment")
        #expect(page.items[0].kind == .bill)
        #expect(page.items[0].cadence == .monthly)
        #expect(page.items[0].amount == money("-450.00"))
        #expect(page.items[0].nextDue == CalendarDate(year: 2026, month: 11, day: 5))
        #expect(page.totals(for: .bill) == SubscriptionTotals(monthly: money("-450.00"), annual: money("-5400.00")))
        #expect(page.totals(for: .subscription) == .zero)
    }

    @Test func missingKindTotalsAreZero() throws {
        let data = Data(#"{"items":[],"totals":{"bill":{"monthly":"-450.00","annual":"-5400.00"}}}"#.utf8)
        let page = try JSONDecoder.api.decode(SubscriptionPage.self, from: data)
        #expect(page.totals(for: .subscription) == .zero)
        #expect(page.totals(for: .bill).monthly == money("-450.00"))
    }

    @Test func unknownCadenceAndKindDecodeAsUnrecognized() throws {
        let row = try decodedSubscription(subscriptionJSON(id: UUID(), cadence: "biweekly", kind: "loan"))
        #expect(row.cadence == .unrecognized("biweekly"))
        #expect(row.kind == .unrecognized("loan"))
        #expect(row.cadence.label == "Biweekly")
    }

    private func subscriptionFields(
        _ name: String, _ cost: String, share: String = "", match: String = "",
        start: CalendarDate? = nil, end: CalendarDate? = nil
    ) -> SubscriptionFields {
        SubscriptionFields(name: name, costText: cost, businessPctText: share, startDate: start, endDate: end, matchText: match)
    }

    private func subscriptionForm(_ fields: SubscriptionFields) throws -> SubscriptionForm {
        try fields.validated.get()
    }

    @Test func subscriptionFormNeedsANameAPositiveCostAndAValidShare() throws {
        func form(
            _ name: String, _ cost: String, share: String = "", match: String = "",
            start: CalendarDate? = nil, end: CalendarDate? = nil
        ) -> SubscriptionForm? {
            try? subscriptionForm(subscriptionFields(name, cost, share: share, match: match, start: start, end: end))
        }
        #expect(form("  ", "9.99") == nil)
        #expect(form(String(repeating: "a", count: 101), "9.99") == nil)
        #expect(form("Netflix", "") == nil)
        #expect(form("Netflix", "12abc") == nil)
        #expect(form("Netflix", "-9.99") == nil)
        #expect(form("Netflix", "0") == nil)
        #expect(form("Netflix", "9.99", share: "101") == nil)
        #expect(form("Netflix", "9.99", share: "-1") == nil)
        #expect(form("Netflix", "9.99", share: "5.5") == nil)
        #expect(form("Netflix", "9.99", match: String(repeating: "a", count: 201)) == nil)
        #expect(form("Netflix", "9.99", start: CalendarDate(year: 2026, month: 5, day: 1), end: CalendarDate(year: 2026, month: 4, day: 1)) == nil)

        let ok = try #require(form("  Netflix ", "1,009.99", share: " 100 ", match: "  "))
        #expect(ok.name == "Netflix")
        #expect(ok.cost == money("1009.99"))
        #expect(ok.amount == money("-1009.99"))
        #expect(ok.businessPct == 100)
        #expect(ok.matchText == nil)
        #expect(try #require(form("Netflix", "9.99", share: "0")).businessPct == 0)
        #expect(try #require(form("Netflix", "9.99")).businessPct == nil)
    }

    @Test func invalidSubscriptionFieldsSayWhichRuleTheyBreak() {
        let april = CalendarDate(year: 2026, month: 4, day: 1)
        let may = CalendarDate(year: 2026, month: 5, day: 1)
        let cases: [(SubscriptionFields, SubscriptionForm.Invalid)] = [
            (subscriptionFields(" ", "9.99"), .name),
            (subscriptionFields(String(repeating: "a", count: 101), "9.99"), .name),
            (subscriptionFields("Netflix", "0"), .cost),
            (subscriptionFields("Netflix", "12abc"), .cost),
            (subscriptionFields("Netflix", "9.99", share: "5.5"), .businessShare),
            (subscriptionFields("Netflix", "9.99", match: String(repeating: "a", count: 201)), .matchText),
            (subscriptionFields("Netflix", "9.99", start: may, end: april), .endBeforeStart),
        ]
        for (fields, rule) in cases {
            #expect(fields.validated == .failure(rule))
        }
        let messages = Set([SubscriptionForm.Invalid.name, .cost, .businessShare, .matchText, .endBeforeStart].map(\.message))
        #expect(messages.count == 5)
        #expect(SubscriptionForm.Invalid.businessShare.message == "Business % must be a whole number from 0 to 100.")
    }

    @Test func subscriptionDraftFromFormSendsANegativeAmount() throws {
        let form = try subscriptionForm(subscriptionFields("Netflix", "15.49"))
        let id = UUID()
        let draft = SubscriptionDraft(id: id, form: form)
        #expect(draft.id == id)
        #expect(draft.amount == money("-15.49"))
        #expect(draft.active)
    }

    @Test func subscriptionDraftFromAnInactiveFormSendsActiveFalse() throws {
        var fields = subscriptionFields("Netflix", "15.49")
        fields.active = false
        let json = try snakeCaseKeys(SubscriptionDraft(id: UUID(), form: subscriptionForm(fields)))
        #expect(json["active"] as? Bool == false)
    }

    @Test func subscriptionFieldsOfARowReadBackAsTheRow() throws {
        let original = try decodedSubscription(subscriptionJSON(id: UUID(), nextDue: nil, active: false, matchText: "NETFLIX"))
        let fields = SubscriptionFields(editing: original)
        #expect(fields.costText == "15.49")
        #expect(!fields.hasNextDue)
        #expect(!fields.active)
        #expect(SubscriptionPatch(from: original, to: try subscriptionForm(fields)) == nil)
    }

    @Test func subscriptionPatchSendsOnlySetKeysAndNullsClears() throws {
        #expect(try snakeCaseKeys(SubscriptionPatch()).isEmpty)
        let cleared = try snakeCaseKeys(SubscriptionPatch(nextDue: .some(nil), businessPct: .some(nil)))
        #expect(Set(cleared.keys) == ["next_due", "business_pct"])
        #expect(cleared["next_due"] is NSNull)
        #expect(cleared["business_pct"] is NSNull)
        let set = try snakeCaseKeys(SubscriptionPatch(amount: money("-9.99"), active: false, businessPct: .some(40)))
        #expect(Set(set.keys) == ["amount", "active", "business_pct"])
        #expect(set["amount"] as? String == "-9.99")
        #expect(set["active"] as? Bool == false)
        #expect(set["business_pct"] as? Int == 40)
    }

    @Test func unchangedSubscriptionFormMakesNoPatch() throws {
        let original = try decodedSubscription(subscriptionJSON(id: UUID()))
        var fields = subscriptionFields("Netflix", "15.49")
        fields.hasNextDue = true
        fields.nextDue = try #require(original.nextDue)
        #expect(SubscriptionPatch(from: original, to: try subscriptionForm(fields)) == nil)
    }

    @Test func storedSpacesAroundTheNameAndMatchTextAreNotAChange() throws {
        let original = try decodedSubscription(subscriptionJSON(id: UUID(), name: " Netflix ", matchText: " NETFLIX "))
        let form = try subscriptionForm(SubscriptionFields(editing: original))
        #expect(form.name == "Netflix")
        #expect(form.matchText == "NETFLIX")
        #expect(SubscriptionPatch(from: original, to: form) == nil)
    }

    @Test func togglingOnlyActiveSendsExactlyActive() throws {
        let original = try decodedSubscription(subscriptionJSON(id: UUID()))
        var fields = SubscriptionFields(editing: original)
        fields.active = false
        let patch = try #require(SubscriptionPatch(from: original, to: try subscriptionForm(fields)))
        #expect(patch == SubscriptionPatch(active: false))
        #expect(try snakeCaseKeys(patch) as NSDictionary == ["active": false] as NSDictionary)
    }

    @Test func subscriptionPatchIsEmptyOnlyWithNoFieldSet() {
        #expect(SubscriptionPatch().isEmpty)
        #expect(!SubscriptionPatch(active: true).isEmpty)
        #expect(!SubscriptionPatch(matchText: .some(nil)).isEmpty)
    }

    @Test func subscriptionPatchFromFormSendsChangedFieldsAndNullsCleared() throws {
        let original = try decodedSubscription(subscriptionJSON(id: UUID()))
        var fields = subscriptionFields("Netflix", "17.99")
        fields.cadence = .yearly
        let form = try subscriptionForm(fields)
        let patch = try #require(SubscriptionPatch(from: original, to: form))
        let json = try snakeCaseKeys(patch)
        #expect(Set(json.keys) == ["amount", "cadence", "next_due"])
        #expect(json["amount"] as? String == "-17.99")
        #expect(json["cadence"] as? String == "yearly")
        #expect(json["next_due"] is NSNull)
    }

    // MARK: Endpoints

    @Test func createSubscriptionPostsExactlyTheSetKeys() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let draft = SubscriptionDraft(name: "Netflix", amount: money("-15.49"), cadence: .monthly, nextDue: CalendarDate(year: 2026, month: 11, day: 5))
        var requests = reply(201, body: Data(subscriptionJSON(id: draft.id).utf8)).makeAsyncIterator()

        let created = try await api.createSubscription(draft)
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "POST")
        #expect(request.url?.path() == "/api/subscriptions")
        let json = try requestJSON(request)
        #expect(Set(json.keys) == ["id", "name", "amount", "cadence", "kind", "bucket", "active", "next_due"])
        #expect(json["id"] as? String == draft.id.uuidString)
        #expect(json["amount"] as? String == "-15.49")
        #expect(json["kind"] as? String == "subscription")
        #expect(created.id == draft.id)
    }

    @Test func updateSubscriptionPatchesTheIdWithTheSetKeys() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let id = UUID()
        var requests = reply(200, body: Data(subscriptionJSON(id: id, active: false).utf8)).makeAsyncIterator()

        let saved = try await api.updateSubscription(id: id, patch: SubscriptionPatch(active: false))
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "PATCH")
        #expect(request.url?.path() == "/api/subscriptions/\(uuidString(id))")
        #expect(try requestJSON(request) as NSDictionary == ["active": false] as NSDictionary)
        #expect(!saved.active)
    }

    @Test func subscriptionsEndpointSendsIncludeInactiveOnlyWhenAsked() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        var requests = reply(200, body: subscriptionPageJSON([])).makeAsyncIterator()

        _ = try await api.subscriptions()
        _ = try await api.subscriptions(includeInactive: true)
        let plain = try #require(await requests.next())
        let all = try #require(await requests.next())
        #expect(plain.url?.path() == "/api/subscriptions")
        #expect(queryItems(plain).isEmpty)
        #expect(queryItems(all) == [URLQueryItem(name: "include_inactive", value: "true")])
    }

    // MARK: Loading

    @Test func subscriptionsSplitByKindAndOrderByNextDue() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<5).map { _ in UUID() }
        let (model, _) = await loadedSubscriptionsModel([
            subscriptionJSON(id: ids[0], name: "Zed", nextDue: "2026-11-02"),
            subscriptionJSON(id: ids[1], name: "Undated", nextDue: nil),
            subscriptionJSON(id: ids[2], name: "Car", kind: "bill", nextDue: "2026-11-09"),
            subscriptionJSON(id: ids[3], name: "Apple", nextDue: "2026-11-02"),
            subscriptionJSON(id: ids[4], name: "Early", nextDue: "2026-10-30"),
        ], bill: "-450.00")

        #expect(model.subscriptions.map(\.id) == [ids[4], ids[3], ids[0], ids[1]])
        #expect(model.bills.map(\.id) == [ids[2]])
        #expect(model.billTotals.monthly == money("-450.00"))
        #expect(model.subscriptionTotals.monthly == money("-15.49"))
        #expect(model.hasLoaded)
        #expect(model.hasData)
    }

    @Test func unrecognizedKindsCountInTheSubscriptionsTotals() async throws {
        defer { try? fixture.cleanUp() }
        let netflix = UUID()
        let loan = UUID()
        let api = makeClient(fixture.make())
        let model = SubscriptionsModel()
        _ = reply(200, body: Data("""
        {"items":[\(subscriptionJSON(id: netflix)),\(subscriptionJSON(id: loan, name: "Loan", amount: "-100.00", kind: "loan"))],\
        "totals":{"subscription":{"monthly":"-15.49","annual":"-185.88"},"loan":{"monthly":"-100.00","annual":"-1200.00"},\
        "bill":{"monthly":"0.00","annual":"0.00"}}}
        """.utf8))
        await model.load(api: api)

        #expect(Set(model.subscriptions.map(\.id)) == [netflix, loan])
        #expect(model.subscriptionTotals == SubscriptionTotals(monthly: money("-115.49"), annual: money("-1385.88")))

        let loanRow = try #require(model.subscriptions.first { $0.id == loan })
        _ = reply(200, body: Data(subscriptionJSON(id: loan, name: "Loan", amount: "-200.00", kind: "loan").utf8))
        await model.update(loanRow, patch: SubscriptionPatch(amount: money("-200.00")), api: api)

        #expect(model.subscriptionTotals == SubscriptionTotals(monthly: money("-215.49"), annual: money("-2585.88")))
        #expect(model.billTotals == .zero)
    }

    @Test func setIncludeInactiveReloadsWithTheQuery() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = SubscriptionsModel()
        var requests = reply(200, body: subscriptionPageJSON([subscriptionJSON(id: UUID(), active: false)])).makeAsyncIterator()

        await model.setIncludeInactive(true, api: api)
        let request = try #require(await requests.next())
        #expect(queryItems(request) == [URLQueryItem(name: "include_inactive", value: "true")])
        #expect(model.includeInactive)
        #expect(model.subscriptions.count == 1)

        _ = reply(200, body: subscriptionPageJSON([]))
        await model.setIncludeInactive(false, api: api)
        #expect(model.subscriptions.isEmpty)
        #expect(!model.includeInactive)
    }

    @Test func staleSubscriptionLoadIsIgnored() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = SubscriptionsModel()
        let (arrived, arrivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let old = subscriptionPageJSON([subscriptionJSON(id: UUID(), name: "Old")])
        let new = subscriptionPageJSON([subscriptionJSON(id: UUID(), name: "New")])
        StubURLProtocol.respond { request in
            if request.url?.query() == nil {
                arrivedSignal.yield()
                for await _ in release { break }
                return (StubURLProtocol.response(to: request, status: 200), old)
            }
            return (StubURLProtocol.response(to: request, status: 200), new)
        }

        let slow = Task { await model.load(api: api) }
        for await _ in arrived { break }
        await model.setIncludeInactive(true, api: api)
        releaseSignal.yield()
        await slow.value

        #expect(model.subscriptions.map(\.name) == ["New"])
    }

    @Test func failedSubscriptionRefreshKeepsRowsAndSetsError() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api) = await loadedSubscriptionsModel([subscriptionJSON(id: UUID())])
        _ = reply(500, body: Data(#"{"error":{"code":"internal","message":"Boom.","request_id":"r2"}}"#.utf8))
        await model.load(api: api)

        #expect(model.subscriptions.count == 1)
        #expect(model.error?.errorDescription == "Boom.")
        #expect(model.hasData)
    }

    @Test func failedFirstSubscriptionLoadHasLoadedButNoData() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = SubscriptionsModel()
        _ = reply(503)
        await model.load(api: api)

        #expect(model.hasLoaded)
        #expect(!model.hasData)
        #expect(model.error?.status == 503)
    }

    // MARK: Create

    @Test func createSubscriptionInsertsInDateOrderAndRecomputesTotals() async throws {
        defer { try? fixture.cleanUp() }
        let later = UUID()
        let (model, api) = await loadedSubscriptionsModel([subscriptionJSON(id: later, name: "Later", amount: "-10.00", nextDue: "2026-11-20")], subscription: "-10.00")

        let draft = SubscriptionDraft(name: "Sooner", amount: money("-120.00"), cadence: .yearly, nextDue: CalendarDate(year: 2026, month: 11, day: 2))
        _ = reply(201, body: Data(subscriptionJSON(id: draft.id, name: "Sooner", amount: "-120.00", cadence: "yearly", nextDue: "2026-11-02").utf8))
        let saved = await model.create(draft, api: api)

        #expect(saved)
        #expect(model.subscriptions.map(\.id) == [draft.id, later])
        #expect(model.subscriptionTotals == SubscriptionTotals(monthly: money("-20.00"), annual: money("-240.00")))
        #expect(model.billTotals == .zero)
    }

    @Test func failedCreateSubscriptionShowsTheMessage() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api) = await loadedSubscriptionsModel([])

        let draft = SubscriptionDraft(name: "Netflix", amount: money("-15.49"), cadence: .monthly)
        _ = reply(422, body: Data(#"{"error":{"code":"validation_error","message":"Bad amount.","request_id":"r5"}}"#.utf8))
        let saved = await model.create(draft, api: api)

        #expect(!saved)
        #expect(model.subscriptions.isEmpty)
        #expect(model.createError?.errorDescription == "Bad amount.")
    }

    @Test func retriedCreateAfterAFailureSendsTheSameId() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api) = await loadedSubscriptionsModel([])
        let form = try subscriptionForm(subscriptionFields("Netflix", "15.49"))
        let submission = DraftSubmission()
        let created = Data(subscriptionJSON(id: submission.draftId).utf8)
        let failed = Data(validationError.body.utf8)
        let attempts = OSAllocatedUnfairLock(initialState: [URLRequest]())
        StubURLProtocol.respond { request in
            let attempt = attempts.withLock { $0.append(request); return $0.count }
            return attempt == 1
                ? (StubURLProtocol.response(to: request, status: 422), failed)
                : (StubURLProtocol.response(to: request, status: 201), created)
        }
        let create = { (id: UUID) in await model.create(SubscriptionDraft(id: id, form: form), api: api) }

        let first = await submission.submit(create, error: { model.createError })
        #expect(!first)
        #expect(submission.failure?.errorDescription == "Nope.")
        #expect(!submission.isSaving)

        let second = await submission.submit(create, error: { model.createError })
        #expect(second)
        #expect(submission.failure == nil)
        let sent = try attempts.withLock { $0 }.map { try requestJSON($0)["id"] as? String }
        #expect(sent.count == 2)
        #expect(sent[0] == sent[1])
        #expect(sent[0] == submission.draftId.uuidString)
        #expect(model.subscriptions.map(\.id) == [submission.draftId])
    }

    @Test func createdSubscriptionOutlivesALoadSentBeforeIt() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api) = await loadedSubscriptionsModel([])
        let draft = SubscriptionDraft(name: "Netflix", amount: money("-15.49"), cadence: .monthly)
        let created = subscriptionJSON(id: draft.id)
        let held = holdLoads(
            page: subscriptionPageJSON([], subscription: "0.00"),
            reloaded: subscriptionPageJSON([created]),
            other: { _ in (201, created) }
        )

        let slow = Task { await model.load(api: api) }
        for await _ in held.arrived { break }
        let saved = await model.create(draft, api: api)
        held.release.yield()
        await slow.value

        #expect(saved)
        #expect(model.subscriptions.map(\.id) == [draft.id])
        #expect(model.subscriptionTotals.monthly == money("-15.49"))
    }

    @Test func createDuringTheShowInactiveReloadStillShowsInactiveRows() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api) = await loadedSubscriptionsModel([])
        let draft = SubscriptionDraft(name: "Netflix", amount: money("-15.49"), cadence: .monthly)
        let created = subscriptionJSON(id: draft.id)
        let closed = subscriptionJSON(id: UUID(), name: "Old", active: false)
        let held = holdLoads(
            page: subscriptionPageJSON([closed]),
            reloaded: subscriptionPageJSON([created, closed]),
            other: { _ in (201, created) }
        )

        let toggle = Task { await model.setIncludeInactive(true, api: api) }
        for await _ in held.arrived { break }
        let saved = await model.create(draft, api: api)
        held.release.yield()
        await toggle.value

        #expect(saved)
        #expect(model.includeInactive)
        #expect(Set(model.subscriptions.map(\.name)) == ["Netflix", "Old"])
    }

    @Test func savedEditOutlivesALoadSentBeforeIt() async throws {
        defer { try? fixture.cleanUp() }
        let id = UUID()
        let (model, api) = await loadedSubscriptionsModel([subscriptionJSON(id: id)])
        let renamed = subscriptionJSON(id: id, name: "Netflix HD")
        let held = holdLoads(
            page: subscriptionPageJSON([subscriptionJSON(id: id)]),
            reloaded: subscriptionPageJSON([renamed]),
            other: { _ in (200, renamed) }
        )

        let slow = Task { await model.load(api: api) }
        for await _ in held.arrived { break }
        let saved = await model.update(model.subscriptions[0], patch: SubscriptionPatch(name: "Netflix HD"), api: api)
        held.release.yield()
        await slow.value

        #expect(saved)
        #expect(model.subscriptions.map(\.name) == ["Netflix HD"])
    }

    @Test func setActiveFalseIsOptimisticThenRollsBackOnFailure() async throws {
        defer { try? fixture.cleanUp() }
        let id = UUID()
        let (model, api) = await loadedSubscriptionsModel([subscriptionJSON(id: id)])

        let held = holdPatches([id: validationError], others: { _ in "{}" })
        var arrived = held.arrived.makeAsyncIterator()
        let row = model.subscriptions[0]
        let inFlight = Task { await model.setActive(row, to: false, api: api) }
        _ = await arrived.next()

        #expect(model.subscriptions.isEmpty)
        #expect(model.subscriptionTotals == .zero)

        held.release[id]?.yield()
        await inFlight.value
        #expect(model.subscriptions.map(\.id) == [id])
        #expect(model.subscriptions[0].active)
        #expect(model.subscriptionTotals.monthly == money("-15.49"))
        #expect(model.editError?.message == "Netflix: Nope.")
    }

    @Test func pausingKeepsTheRowWhenInactiveAreShownButDropsItFromTotals() async throws {
        defer { try? fixture.cleanUp() }
        let id = UUID()
        let (model, api) = await loadedSubscriptionsModel([subscriptionJSON(id: id)], includeInactive: true)

        _ = reply(200, body: Data(subscriptionJSON(id: id, active: false).utf8))
        await model.setActive(model.subscriptions[0], to: false, api: api)

        #expect(model.subscriptions.count == 1)
        #expect(!model.subscriptions[0].active)
        #expect(model.subscriptionTotals == .zero)
        #expect(model.editError == nil)
    }

    @Test func subscriptionUpdateShowsTheServersRowAndRecomputesTotals() async throws {
        defer { try? fixture.cleanUp() }
        let id = UUID()
        let (model, api) = await loadedSubscriptionsModel([subscriptionJSON(id: id, cadence: "weekly")])

        _ = reply(200, body: Data(subscriptionJSON(id: id, name: "Netflix HD", amount: "-12.00", cadence: "weekly").utf8))
        await model.update(model.subscriptions[0], patch: SubscriptionPatch(amount: money("-12.00")), api: api)

        #expect(model.subscriptions[0].name == "Netflix HD")
        #expect(model.subscriptionTotals == SubscriptionTotals(monthly: money("-52.00"), annual: money("-624.00")))
    }
}
