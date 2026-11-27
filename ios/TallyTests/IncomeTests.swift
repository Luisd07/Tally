import Foundation
import os
import Testing
@testable import Tally

/// Lives in `APIClientTests`' serialized suite because the network tests share `StubURLProtocol`'s one handler.
extension APIClientTests {
    private func incomeJSON(
        id: UUID,
        date: String = "2026-10-12",
        type: String = "event_pay",
        description: String? = "Gig",
        expected: String? = "400.00",
        received: String? = nil
    ) -> String {
        let quote: (String?) -> String = { $0.map { "\"\($0)\"" } ?? "null" }
        return """
        {"id":"\(uuidString(id))","date":"\(date)","type":"\(type)","description":\(quote(description)),\
        "expected":\(quote(expected)),"received":\(quote(received)),"transaction_id":null,"event_id":null,\
        "created_at":"2026-10-01T00:00:00Z","updated_at":"2026-10-01T00:00:00Z"}
        """
    }

    private func incomePageJSON(_ rows: [String], expected: String, received: String) -> Data {
        Data(#"{"items":[\#(rows.joined(separator: ","))],"totals":{"expected":"\#(expected)","received":"\#(received)"}}"#.utf8)
    }

    private func entry(expected: String?, received: String?) throws -> IncomeEntry {
        try JSONDecoder.api.decode(IncomeEntry.self, from: Data(incomeJSON(id: UUID(), expected: expected, received: received).utf8))
    }

    private func json(_ request: URLRequest) throws -> [String: Any] {
        let body = try #require(bodyData(request))
        return try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
    }

    /// An IncomeModel showing October 2026 with `rows`; the totals are the rows' sums as the server sends them.
    private func loadedIncomeModel(_ rows: [String], expected: String = "400.00", received: String = "0.00") async -> (IncomeModel, APIClient) {
        let api = makeClient(fixture.make())
        let model = IncomeModel()
        _ = reply(200, body: incomePageJSON(rows, expected: expected, received: received))
        await model.load(month: MonthRef(year: 2026, month: 10), api: api)
        return (model, api)
    }

    // MARK: Models

    @Test func incomeFixtureDecodesWithTotals() throws {
        let page = try JSONDecoder.api.decode(IncomePage.self, from: jsonFixture("income-2026-10"))
        #expect(page.items.count == 2)
        #expect(page.items[0].type == .reimbursement)
        #expect(page.items[0].received == nil)
        #expect(page.items[1].received == money("1500.00"))
        #expect(page.totals.expected == money("1750.00"))
        #expect(page.totals.received == money("1500.00"))
    }

    @Test func incomePatchSendsOnlySetKeysAndNullsClears() throws {
        func keys(_ patch: IncomePatch) throws -> [String: Any] {
            let data = try JSONEncoder().encode(patch)
            return try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
        }
        #expect(try keys(IncomePatch()).isEmpty)
        let cleared = try keys(IncomePatch(received: .some(nil)))
        #expect(Set(cleared.keys) == ["received"])
        #expect(cleared["received"] is NSNull)
        let set = try keys(IncomePatch(date: CalendarDate(year: 2026, month: 10, day: 9), type: .other, expected: .some(money("5"))))
        #expect(Set(set.keys) == ["date", "type", "expected"])
        #expect(set["expected"] as? String == "5.00")
        #expect(set["type"] as? String == "other")
    }

    @Test func fullyReceivedNeedsTheExpectedAmountOrNoExpectation() throws {
        #expect(try entry(expected: "400.00", received: "400.00").isFullyReceived)
        #expect(try entry(expected: "400.00", received: "450.00").isFullyReceived)
        #expect(try !entry(expected: "400.00", received: "100.00").isFullyReceived)
        #expect(try !entry(expected: "400.00", received: nil).isFullyReceived)
        #expect(try entry(expected: nil, received: "100.00").isFullyReceived)
    }

    @Test func incomeFormNeedsAValidAmount() throws {
        let day = CalendarDate(year: 2026, month: 10, day: 12)
        func form(_ expected: String, _ received: String, description: String = "") -> IncomeForm? {
            IncomeForm(date: day, type: .eventPay, description: description, expectedText: expected, receivedText: received)
        }
        #expect(form("", "") == nil)
        #expect(form("12abc", "") == nil)
        #expect(form("400", "1.2.3") == nil)
        let grouped = try #require(form("1,000", " "))
        #expect(grouped.expected == money("1000"))
        #expect(grouped.received == nil)
        let receivedOnly = try #require(form(" ", "50", description: "  Gig  "))
        #expect(receivedOnly.expected == nil)
        #expect(receivedOnly.received == money("50"))
        #expect(receivedOnly.description == "Gig")
        #expect(try #require(form("", "50", description: "   ")).description == nil)
    }

    @Test func unchangedIncomeFormMakesNoPatch() throws {
        let original = try entry(expected: "400.00", received: nil)
        let form = IncomeForm(date: original.date, type: original.type, description: "Gig", expected: money("400"), received: nil)
        #expect(IncomePatch(from: original, to: form) == nil)
    }

    @Test func incomePatchFromFormSendsOnlyChangedFieldsAndNullsCleared() throws {
        let original = try entry(expected: "400.00", received: "400.00")
        let form = IncomeForm(date: original.date, type: .remoteHours, description: nil, expected: money("400"), received: nil)
        let patch = try #require(IncomePatch(from: original, to: form))
        let json = try #require(JSONSerialization.jsonObject(with: JSONEncoder().encode(patch)) as? [String: Any])
        #expect(Set(json.keys) == ["type", "description", "received"])
        #expect(json["type"] as? String == "remote_hours")
        #expect(json["description"] is NSNull)
        #expect(json["received"] is NSNull)
    }

    // MARK: Endpoints

    @Test func createIncomePostsExactlyTheSetKeys() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let draft = IncomeDraft(date: CalendarDate(year: 2026, month: 10, day: 12), type: .eventPay, expected: money("400"))
        var requests = reply(201, body: Data(incomeJSON(id: draft.id).utf8)).makeAsyncIterator()

        let created = try await api.createIncome(draft)
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "POST")
        #expect(request.url?.path() == "/api/income")
        let json = try json(request)
        #expect(Set(json.keys) == ["id", "date", "type", "expected"])
        #expect(json["id"] as? String == draft.id.uuidString)
        #expect(json["expected"] as? String == "400.00")
        #expect(created.id == draft.id)
    }

    @Test func incomeEndpointSendsTheTypeWhenGiven() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        var requests = reply(200, body: incomePageJSON([], expected: "0.00", received: "0.00")).makeAsyncIterator()

        _ = try await api.income(month: MonthRef(year: 2026, month: 10), type: .remoteHours)
        let request = try #require(await requests.next())
        #expect(queryItems(request) == [URLQueryItem(name: "month", value: "2026-10"), URLQueryItem(name: "type", value: "remote_hours")])
    }

    // MARK: Loading

    @Test func incomeModelSendsTheMonth() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = IncomeModel()
        var requests = reply(200, body: incomePageJSON([], expected: "0.00", received: "0.00")).makeAsyncIterator()

        await model.load(month: MonthRef(year: 2026, month: 9), api: api)
        let request = try #require(await requests.next())
        #expect(request.url?.path() == "/api/income")
        #expect(queryItems(request) == [URLQueryItem(name: "month", value: "2026-09")])
        #expect(model.hasLoaded)
        #expect(model.hasData)
    }

    @Test func incomeModelIgnoresAStaleMonthResponse() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = IncomeModel()
        let (arrived, arrivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let october = incomePageJSON([incomeJSON(id: UUID())], expected: "400.00", received: "0.00")
        let november = incomePageJSON([], expected: "0.00", received: "0.00")
        StubURLProtocol.respond { request in
            if request.url?.query()?.contains("2026-10") == true {
                arrivedSignal.yield()
                for await _ in release { break }
                return (StubURLProtocol.response(to: request, status: 200), october)
            }
            return (StubURLProtocol.response(to: request, status: 200), november)
        }

        let slow = Task { await model.load(month: MonthRef(year: 2026, month: 10), api: api) }
        for await _ in arrived { break }
        await model.load(month: MonthRef(year: 2026, month: 11), api: api)
        releaseSignal.yield()
        await slow.value

        #expect(model.entries.isEmpty)
        #expect(model.totals == .zero)
    }

    @Test func failedIncomeRefreshKeepsRowsAndSetsError() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api) = await loadedIncomeModel([incomeJSON(id: UUID())])
        _ = reply(500, body: Data(#"{"error":{"code":"internal","message":"Boom.","request_id":"r2"}}"#.utf8))
        await model.load(month: MonthRef(year: 2026, month: 10), api: api)

        #expect(model.entries.count == 1)
        #expect(model.error?.errorDescription == "Boom.")
        #expect(model.hasData)
    }

    @Test func failedFirstIncomeLoadHasLoadedButNoData() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = IncomeModel()
        _ = reply(503)
        await model.load(month: MonthRef(year: 2026, month: 10), api: api)

        #expect(model.hasLoaded)
        #expect(!model.hasData)
        #expect(model.error?.status == 503)
    }

    @Test func incomeGroupsKeepTheOrderEachTypeFirstAppears() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<3).map { _ in UUID() }
        let (model, _) = await loadedIncomeModel([
            incomeJSON(id: ids[0], date: "2026-10-20", type: "remote_hours"),
            incomeJSON(id: ids[1], date: "2026-10-12"),
            incomeJSON(id: ids[2], date: "2026-10-04", type: "remote_hours"),
        ])

        #expect(model.groups.map(\.type) == [.remoteHours, .eventPay])
        #expect(model.groups.map { $0.entries.map(\.id) } == [[ids[0], ids[2]], [ids[1]]])
    }

    // MARK: Create

    @Test func createIncomeInsertsInDateOrderAndRecomputesTotals() async throws {
        defer { try? fixture.cleanUp() }
        let older = UUID()
        let (model, api) = await loadedIncomeModel([incomeJSON(id: older, date: "2026-10-04")])

        let draft = IncomeDraft(date: CalendarDate(year: 2026, month: 10, day: 20), type: .eventPay, expected: money("100"))
        _ = reply(201, body: Data(incomeJSON(id: draft.id, date: "2026-10-20", expected: "100.00").utf8))
        let saved = await model.create(draft, api: api)

        #expect(saved)
        #expect(model.entries.map(\.id) == [draft.id, older])
        #expect(model.totals.expected == money("500"))
    }

    @Test func createIncomeDatedInAnotherMonthSucceedsUnlisted() async throws {
        defer { try? fixture.cleanUp() }
        let listed = UUID()
        let (model, api) = await loadedIncomeModel([incomeJSON(id: listed)])

        let draft = IncomeDraft(date: CalendarDate(year: 2026, month: 11, day: 2), type: .eventPay, expected: money("100"))
        _ = reply(201, body: Data(incomeJSON(id: draft.id, date: "2026-11-02", expected: "100.00").utf8))
        let saved = await model.create(draft, api: api)

        #expect(saved)
        #expect(model.entries.map(\.id) == [listed])
        #expect(model.totals.expected == money("400"))
        #expect(model.createError == nil)
    }

    @Test func failedCreateIncomeShowsTheMessage() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api) = await loadedIncomeModel([], expected: "0.00")

        let draft = IncomeDraft(date: CalendarDate(year: 2026, month: 10, day: 20), type: .other, received: money("50"))
        _ = reply(422, body: Data(#"{"error":{"code":"validation_error","message":"Bad amount.","request_id":"r5"}}"#.utf8))
        let saved = await model.create(draft, api: api)

        #expect(!saved)
        #expect(model.entries.isEmpty)
        #expect(model.createError?.errorDescription == "Bad amount.")
    }

    @Test func retriedIncomeCreateAfterAFailureSendsTheSameId() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api) = await loadedIncomeModel([], expected: "0.00")
        let form = IncomeForm(date: CalendarDate(year: 2026, month: 10, day: 20), type: .other, expected: nil, received: money("50"))
        let submission = DraftSubmission()
        let created = Data(incomeJSON(id: submission.draftId, date: "2026-10-20", expected: nil, received: "50.00").utf8)
        let failed = Data(#"{"error":{"code":"validation_error","message":"Nope.","request_id":"r5"}}"#.utf8)
        let attempts = OSAllocatedUnfairLock(initialState: [URLRequest]())
        StubURLProtocol.respond { request in
            let attempt = attempts.withLock { $0.append(request); return $0.count }
            return attempt == 1
                ? (StubURLProtocol.response(to: request, status: 422), failed)
                : (StubURLProtocol.response(to: request, status: 201), created)
        }
        let create = { (id: UUID) in await model.create(IncomeDraft(id: id, form: form), api: api) }

        let first = await submission.submit(create, error: { model.createError })
        #expect(!first)
        #expect(submission.failure?.errorDescription == "Nope.")

        let second = await submission.submit(create, error: { model.createError })
        #expect(second)
        let sent = try attempts.withLock { $0 }.map { try json($0)["id"] as? String }
        #expect(sent.count == 2)
        #expect(sent[0] == sent[1])
        #expect(sent[0] == submission.draftId.uuidString)
        #expect(model.entries.map(\.id) == [submission.draftId])
    }

    @Test func createdIncomeOutlivesAMonthLoadSentBeforeIt() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api) = await loadedIncomeModel([])
        let draft = IncomeDraft(date: CalendarDate(year: 2026, month: 10, day: 20), type: .eventPay, expected: money("100"))
        let created = incomeJSON(id: draft.id, date: "2026-10-20", expected: "100.00")
        let stale = incomePageJSON([], expected: "0.00", received: "0.00")
        let fresh = incomePageJSON([created], expected: "100.00", received: "0.00")
        let (arrived, arrivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let gets = OSAllocatedUnfairLock(initialState: 0)
        StubURLProtocol.respond { request in
            guard request.httpMethod == "GET" else {
                return (StubURLProtocol.response(to: request, status: 201), Data(created.utf8))
            }
            guard gets.withLock({ $0 += 1; return $0 }) == 1 else {
                return (StubURLProtocol.response(to: request, status: 200), fresh)
            }
            arrivedSignal.yield()
            for await _ in release { break }
            return (StubURLProtocol.response(to: request, status: 200), stale)
        }

        let slow = Task { await model.load(month: MonthRef(year: 2026, month: 10), api: api) }
        for await _ in arrived { break }
        let saved = await model.create(draft, api: api)
        releaseSignal.yield()
        await slow.value

        #expect(saved)
        #expect(model.entries.map(\.id) == [draft.id])
        #expect(model.totals.expected == money("100"))
    }

    // MARK: Edit

    @Test func incomeRefreshInFlightDuringAnEditKeepsTheEdit() async throws {
        defer { try? fixture.cleanUp() }
        let id = UUID()
        let (model, api) = await loadedIncomeModel([incomeJSON(id: id)])
        let received = incomeJSON(id: id, received: "400.00")
        let held = holdLoads(
            page: incomePageJSON([incomeJSON(id: id)], expected: "400.00", received: "0.00"),
            reloaded: incomePageJSON([received], expected: "400.00", received: "400.00"),
            other: { _ in (200, received) }
        )

        let slow = Task { await model.load(month: MonthRef(year: 2026, month: 10), api: api) }
        for await _ in held.arrived { break }
        await model.markReceived(model.entries[0], api: api)
        held.release.yield()
        await slow.value

        #expect(model.entries.map(\.id) == [id])
        #expect(model.entries[0].received == money("400"))
        #expect(model.totals.received == money("400"))
        #expect(model.editError == nil)
    }

    @Test func markReceivedIsOptimisticThenRollsBackOnFailure() async throws {
        defer { try? fixture.cleanUp() }
        let id = UUID()
        let (model, api) = await loadedIncomeModel([incomeJSON(id: id)])

        let (received, receivedSignal) = AsyncStream.makeStream(of: URLRequest.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let envelope = #"{"error":{"code":"validation_error","message":"Nope.","request_id":"r1"}}"#
        StubURLProtocol.respond { request in
            receivedSignal.yield(request)
            for await _ in release { break }
            return (StubURLProtocol.response(to: request, status: 422), Data(envelope.utf8))
        }
        let entry = model.entries[0]
        let inFlight = Task { await model.markReceived(entry, api: api) }
        var requests = received.makeAsyncIterator()
        let request = try #require(await requests.next())

        #expect(request.httpMethod == "PATCH")
        #expect(try json(request) as NSDictionary == ["received": "400.00"] as NSDictionary)
        #expect(model.entries[0].received == money("400"))
        #expect(model.totals.received == money("400"))
        #expect(model.remaining == Money(0))

        releaseSignal.yield()
        await inFlight.value
        #expect(model.entries[0].received == nil)
        #expect(model.totals.received == Money(0))
        #expect(model.remaining == money("400"))
        #expect(model.editError?.message == "Gig: Nope.")
    }

    @Test func incomeUpdateShowsTheServersRowAndTotals() async throws {
        defer { try? fixture.cleanUp() }
        let id = UUID()
        let (model, api) = await loadedIncomeModel([incomeJSON(id: id)])

        let server = incomeJSON(id: id, description: "Gig, tipped", expected: "500.00", received: "120.00")
        _ = reply(200, body: Data(server.utf8))
        await model.update(model.entries[0], patch: IncomePatch(expected: .some(money("500"))), api: api)

        #expect(model.entries[0].description == "Gig, tipped")
        #expect(model.entries[0].received == money("120"))
        #expect(model.totals == IncomeTotals(expected: money("500"), received: money("120")))
        #expect(model.editError == nil)
    }

    @Test func clearedIncomeFieldsAreSentAsNull() async throws {
        defer { try? fixture.cleanUp() }
        let id = UUID()
        let (model, api) = await loadedIncomeModel([incomeJSON(id: id, received: "400.00")], received: "400.00")
        let original = model.entries[0]
        let form = try #require(IncomeForm(date: original.date, type: original.type, description: " ", expectedText: "400.00", receivedText: ""))
        let patch = try #require(IncomePatch(from: original, to: form))

        var requests = reply(200, body: Data(incomeJSON(id: id, description: nil).utf8)).makeAsyncIterator()
        await model.update(original, patch: patch, api: api)
        let request = try #require(await requests.next())

        let json = try json(request)
        #expect(Set(json.keys) == ["description", "received"])
        #expect(json["description"] is NSNull)
        #expect(json["received"] is NSNull)
        #expect(model.entries[0].received == nil)
        #expect(model.totals.received == Money(0))
    }

    @Test func editingIncomeIntoAnotherMonthRemovesTheRow() async throws {
        defer { try? fixture.cleanUp() }
        let moved = UUID()
        let kept = UUID()
        let (model, api) = await loadedIncomeModel(
            [incomeJSON(id: moved, date: "2026-10-20"), incomeJSON(id: kept, expected: "100.00")],
            expected: "500.00"
        )

        let held = holdPatches([moved: (status: 200, body: incomeJSON(id: moved, date: "2026-11-02"))], others: { _ in "{}" })
        var arrived = held.arrived.makeAsyncIterator()
        let patch = IncomePatch(date: CalendarDate(year: 2026, month: 11, day: 2))
        let inFlight = Task { await model.update(model.entries[0], patch: patch, api: api) }
        _ = await arrived.next()
        #expect(model.entries.map(\.id) == [kept])
        #expect(model.totals.expected == money("100"))

        held.release[moved]?.yield()
        await inFlight.value
        #expect(model.entries.map(\.id) == [kept])
        #expect(model.totals.expected == money("100"))
        #expect(model.editError == nil)
    }

    @Test func movingIncomeWithinTheMonthKeepsDateOrder() async throws {
        defer { try? fixture.cleanUp() }
        let later = UUID()
        let earlier = UUID()
        let (model, api) = await loadedIncomeModel([incomeJSON(id: later, date: "2026-10-20"), incomeJSON(id: earlier, date: "2026-10-04")])

        _ = reply(200, body: Data(incomeJSON(id: earlier, date: "2026-10-28").utf8))
        await model.update(model.entries[1], patch: IncomePatch(date: CalendarDate(year: 2026, month: 10, day: 28)), api: api)

        #expect(model.entries.map(\.id) == [earlier, later])
    }

    @Test func incomeEditFailureAfterAMonthSwitchStillShowsTheError() async throws {
        defer { try? fixture.cleanUp() }
        let id = UUID()
        let (model, api) = await loadedIncomeModel([incomeJSON(id: id)])
        let november = String(decoding: incomePageJSON([], expected: "0.00", received: "0.00"), as: UTF8.self)

        let held = holdPatches([id: validationError], others: { _ in november })
        var arrived = held.arrived.makeAsyncIterator()
        let entry = model.entries[0]
        let inFlight = Task { await model.markReceived(entry, api: api) }
        _ = await arrived.next()
        await model.load(month: MonthRef(year: 2026, month: 11), api: api)

        held.release[id]?.yield()
        await inFlight.value
        #expect(model.editError?.message == "Gig: Nope.")
        #expect(model.entries.isEmpty)
        #expect(model.totals == .zero)
    }

    @Test func cancelledIncomeEditRollsBackWithoutAnError() async throws {
        defer { try? fixture.cleanUp() }
        let id = UUID()
        let (model, api) = await loadedIncomeModel([incomeJSON(id: id)])

        let (received, receivedSignal) = AsyncStream.makeStream(of: Void.self)
        StubURLProtocol.respond { request in
            receivedSignal.yield()
            try await Task.sleep(for: .seconds(30))
            return (StubURLProtocol.response(to: request, status: 200), Data())
        }
        let entry = model.entries[0]
        let inFlight = Task { await model.markReceived(entry, api: api) }
        for await _ in received { break }
        inFlight.cancel()
        await inFlight.value

        #expect(model.editError == nil)
        #expect(model.entries[0].received == nil)
        #expect(model.totals.received == Money(0))
    }

    /// Two PATCHes on one row, held until released in order; each answers with its `outcomes` entry
    /// (a 200 with the given received amount, or a 422 for nil). Returns the row once both have finished.
    private func overlappingIncomeEdits(_ outcomes: [String?]) async throws -> (entry: IncomeEntry, model: IncomeModel) {
        let id = UUID()
        let (model, api) = await loadedIncomeModel([incomeJSON(id: id, description: "Gig")])
        let replies = outcomes.map { received in
            received.map { (status: 200, body: incomeJSON(id: id, description: "Gig \($0)", received: $0)) } ?? validationError
        }
        let count = OSAllocatedUnfairLock(initialState: 0)
        let (arrived, arrivedSignal) = AsyncStream.makeStream(of: Void.self)
        let gates = replies.map { _ in AsyncStream.makeStream(of: Void.self) }
        let waiting = gates.map(\.stream)
        StubURLProtocol.respond { request in
            let index = count.withLock { count in
                defer { count += 1 }
                return count
            }
            arrivedSignal.yield()
            for await _ in waiting[index] { break }
            return (StubURLProtocol.response(to: request, status: replies[index].status), Data(replies[index].body.utf8))
        }
        var arrivals = arrived.makeAsyncIterator()
        var edits: [Task<Void, Never>] = []
        for amount in ["100.00", "200.00"] {
            let patch = IncomePatch(description: .some("Gig \(amount)"), received: .some(money(amount)))
            let entry = model.entries[0]
            edits.append(Task { await model.update(entry, patch: patch, api: api) })
            _ = await arrivals.next()
        }
        #expect(model.entries[0].received == money("200"))

        for (gate, edit) in zip(gates, edits) {
            gate.continuation.yield()
            await edit.value
        }
        return (model.entries[0], model)
    }

    @Test func overlappingIncomeEditsSettleOnTheLaterServerReply() async throws {
        defer { try? fixture.cleanUp() }
        let (entry, model) = try await overlappingIncomeEdits([nil, "200.00"])
        #expect(entry.received == money("200"))
        #expect(entry.description == "Gig 200.00")
        #expect(model.totals.received == money("200"))
    }

    @Test func overlappingIncomeEditsThatBothFailRestoreTheServersRow() async throws {
        defer { try? fixture.cleanUp() }
        let (entry, model) = try await overlappingIncomeEdits([nil, nil])
        #expect(entry.received == nil)
        #expect(entry.description == "Gig")
        #expect(model.totals.received == Money(0))
        #expect(model.editError?.message == "Gig 100.00: Nope.")
    }

    @Test func overlappingIncomeEditsKeepTheEarlierReplyWhenTheLaterFails() async throws {
        defer { try? fixture.cleanUp() }
        let (entry, model) = try await overlappingIncomeEdits(["100.00", nil])
        #expect(entry.received == money("100"))
        #expect(entry.description == "Gig 100.00")
        #expect(model.totals.received == money("100"))
    }
}
