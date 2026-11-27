import Foundation
import os
import Testing
@testable import Tally

/// A stand-in server for the Reimbursements screen: answers the reads from its current state, records every
/// request, and answers each write with `onWrite`.
private final class FakeReimbursementServer: Sendable {
    struct State {
        var reports = #"{"items":[]}"#
        var owed = #"{"owed":"0.00"}"#
        var transactions = #"{"items":[],"next_cursor":null}"#
        /// The transaction pages after the first, by the cursor that asks for each.
        var laterTransactionPages: [String: String] = [:]
        var income = #"{"items":[],"totals":{"expected":"0.00","received":"0.00"}}"#
        var requests: [URLRequest] = []
        var failReads = false
    }

    private let state = OSAllocatedUnfairLock(initialState: State())

    typealias OnWrite = @Sendable (URLRequest, FakeReimbursementServer) async -> (Int, String)

    init(onWrite: @escaping OnWrite = { _, _ in (500, "") }) {
        StubURLProtocol.respond { [self] request in
            state.withLock { $0.requests.append(request) }
            let (status, body): (Int, String)
            if request.httpMethod == "GET" {
                (status, body) = read(request)
            } else {
                (status, body) = await onWrite(request, self)
            }
            return (StubURLProtocol.response(to: request, status: status), Data(body.utf8))
        }
    }

    func update(_ change: @Sendable (inout State) -> Void) {
        state.withLock { change(&$0) }
    }

    var requests: [URLRequest] { state.withLock { $0.requests } }

    func requests(_ method: String, _ path: String) -> [URLRequest] {
        requests.filter { $0.httpMethod == method && $0.url?.path() == path }
    }

    private func read(_ request: URLRequest) -> (Int, String) {
        let cursor = request.url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: false) }?
            .queryItems?.first { $0.name == "cursor" }?.value
        return state.withLock { state in
            if state.failReads { return (500, #"{"error":{"code":"internal","message":"Boom.","request_id":"r9"}}"#) }
            switch request.url?.path() ?? "" {
            case "/api/reimbursements": return (200, state.reports)
            case "/api/reimbursements/owed": return (200, state.owed)
            case "/api/transactions":
                guard let cursor else { return (200, state.transactions) }
                return state.laterTransactionPages[cursor].map { (200, $0) } ?? (404, "")
            case "/api/income": return (200, state.income)
            default: return (404, "")
            }
        }
    }
}

/// JSON the tests' stub server answers with.
private enum ReimbursementJSON {
    static func uuid(_ id: UUID) -> String { id.uuidString.lowercased() }

    static let draftId = UUID(uuidString: "A03FBB7C-D0E2-5C9A-95CE-BEDFB9B6F8A6")!
    static let submittedId = UUID(uuidString: "3F182FF7-8527-5FAB-ADCE-CCB0F9C20035")!
    static let paidId = UUID(uuidString: "3CDB5571-F639-5511-ADB3-57DB57CACC85")!

    static func report(
        _ id: UUID,
        status: String,
        note: String? = nil,
        submitted: String? = nil,
        paid: String? = nil,
        income: UUID? = nil,
        total: String = "0.00",
        count: Int = 0,
        created: String = "2026-10-18T20:42:06Z"
    ) -> String {
        let quote: (String?) -> String = { $0.map { "\"\($0)\"" } ?? "null" }
        return """
        {"id":"\(uuid(id))","status":"\(status)","submitted_date":\(quote(submitted)),"paid_date":\(quote(paid)),\
        "income_entry_id":\(quote(income.map(uuid))),"note":\(quote(note)),"total":"\(total)",\
        "transaction_count":\(count),"created_at":"\(created)","updated_at":"\(created)"}
        """
    }

    static func page(_ reports: String...) -> String {
        #"{"items":[\#(reports.joined(separator: ","))]}"#
    }

    static func expense(_ id: UUID, amount: String = "-35.00", report: UUID? = nil, pending: Bool = false) -> String {
        """
        {"id":"\(uuid(id))","account_id":"\(uuid(UUID()))","posted_date":"2026-10-07","transacted_at":null,\
        "amount":"\(amount)","description_raw":"AUTOZONE","merchant":null,"category_id":null,"bucket":"reimbursable",\
        "source":"manual","external_id":null,"pending":\(pending),"reviewed":false,"notes":null,"event_id":null,\
        "parent_id":null,"reimbursement_id":\(report.map { "\"\(uuid($0))\"" } ?? "null"),"is_split":false,\
        "created_at":"2026-10-18T20:42:06Z","updated_at":"2026-10-18T20:42:06Z"}
        """
    }

    static func incomeEntry(_ id: UUID) -> String {
        """
        {"id":"\(uuid(id))","date":"2026-10-02","type":"reimbursement","description":null,"expected":null,\
        "received":"60.00","transaction_id":null,"event_id":null,"created_at":"2026-10-18T20:42:06Z",\
        "updated_at":"2026-10-18T20:42:06Z"}
        """
    }

    static func envelope(_ message: String) -> String {
        #"{"error":{"code":"conflict","message":"\#(message)","request_id":"r1"}}"#
    }
}

/// Lives in `APIClientTests`' serialized suite because the network tests share `StubURLProtocol`'s one handler.
extension APIClientTests {
    private func jsonBody(_ request: URLRequest) throws -> [String: Any] {
        let data = try #require(bodyData(request))
        return try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private var oct19: CalendarDate { CalendarDate(year: 2026, month: 10, day: 19) }

    /// A model loaded from a server holding a draft, a submitted and a paid report.
    private func loadedReimbursementsModel(
        onWrite: @escaping FakeReimbursementServer.OnWrite = { _, _ in (500, "") }
    ) async -> (ReimbursementsModel, APIClient, FakeReimbursementServer) {
        let server = FakeReimbursementServer(onWrite: onWrite)
        server.update {
            $0.reports = ReimbursementJSON.page(
                ReimbursementJSON.report(ReimbursementJSON.draftId, status: "draft", note: "Pit crew tools and fuel"),
                ReimbursementJSON.report(
                    ReimbursementJSON.submittedId, status: "submitted", note: "October race weekend",
                    submitted: "2026-10-10", total: "85.00", count: 1
                ),
                ReimbursementJSON.report(
                    ReimbursementJSON.paidId, status: "paid", note: "September test day",
                    submitted: "2026-09-25", paid: "2026-10-02", income: UUID(), total: "60.00", count: 1
                )
            )
            $0.owed = #"{"owed":"120.00"}"#
        }
        let api = makeClient(fixture.make())
        let model = ReimbursementsModel()
        await model.load(api: api)
        return (model, api, server)
    }

    // MARK: Models

    @Test func reimbursementsFixtureDecodesToTheListedValues() throws {
        let reports = try JSONDecoder.api.decode(ReimbursementPage.self, from: jsonFixture("reimbursements")).items
        #expect(reports.map(\.status) == [.draft, .submitted, .paid])
        #expect(reports.map(\.total) == [money("0.00"), money("85.00"), money("60.00")])
        #expect(reports.map(\.transactionCount) == [0, 1, 1])
        #expect(reports[0].note == "Pit crew tools and fuel")
        #expect(reports[0].submittedDate == nil)
        #expect(reports[1].submittedDate == CalendarDate(year: 2026, month: 10, day: 10))
        #expect(reports[2].paidDate == CalendarDate(year: 2026, month: 10, day: 2))
        #expect(reports[2].incomeEntryId == UUID(uuidString: "7762d9ad-b08f-51c0-a213-b90aa6783644"))
        #expect(reports[0].isDraft && !reports[1].isDraft)
    }

    @Test func owedFixtureDecodes() throws {
        #expect(try JSONDecoder.api.decode(Owed.self, from: jsonFixture("owed")).owed == money("120.00"))
    }

    @Test func statusStepsForwardOneAtATimeAndUnknownOnesKeepTheirWireString() throws {
        #expect(ReimbursementStatus.draft.next == .submitted)
        #expect(ReimbursementStatus.submitted.next == .paid)
        #expect(ReimbursementStatus.paid.next == nil)
        #expect(ReimbursementStatus.unrecognized("void").next == nil)
        #expect(ReimbursementStatus.allKnown.map(\.label) == ["Draft", "Submitted", "Paid"])
        let unknown = try JSONDecoder.api.decode([ReimbursementStatus].self, from: Data(#"["void"]"#.utf8))
        #expect(unknown == [.unrecognized("void")])
    }

    @Test func eachStatusOffersItsOneStepAndAnUnnamedReportHasOneName() throws {
        #expect(ReimbursementStatus.draft.step == .submit)
        #expect(ReimbursementStatus.submitted.step == .markPaid)
        #expect(ReimbursementStatus.paid.step == nil)
        #expect(ReimbursementStatus.unrecognized("void").step == nil)
        #expect([ReimbursementStep.submit, .markPaid].map(\.title) == ["Submit", "Mark paid"])
        let reports = try JSONDecoder.api.decode(ReimbursementPage.self, from: jsonFixture("reimbursements")).items
        let unnamed = reports[0].applying(ReimbursementPatch(note: .some(nil)))
        #expect(unnamed.displayName == "Untitled report")
    }

    @Test func reportNoteIsTrimmedAndCountedInUnicodeScalars() {
        #expect(ReimbursementForm.validated(noteText: "  ") == .success(ReimbursementForm(note: nil)))
        #expect(ReimbursementForm.validated(noteText: " Trip ") == .success(ReimbursementForm(note: "Trip")))
        let longest = String(repeating: "a", count: ReimbursementForm.maxNoteLength)
        #expect(ReimbursementForm.validated(noteText: longest) == .success(ReimbursementForm(note: longest)))
        #expect(ReimbursementForm.validated(noteText: longest + "a") == .failure(.noteTooLong))
        // 100 characters, 700 code points: the server counts the code points.
        let families = String(repeating: "👨‍👩‍👧‍👦", count: 100)
        #expect(families.count == 100)
        #expect(ReimbursementForm.validated(noteText: families) == .failure(.noteTooLong))
    }

    @Test func draftSendsOnlyTheKeysItSets() throws {
        let id = UUID()
        #expect(Set(try snakeCaseKeys(ReimbursementDraft(id: id)).keys) == ["id"])
        let ids = [UUID()]
        let full = try snakeCaseKeys(ReimbursementDraft(id: id, note: "Trip", transactionIds: ids))
        #expect(Set(full.keys) == ["id", "note", "transaction_ids"])
        #expect(full["id"] as? String == id.uuidString)
        #expect(full["transaction_ids"] as? [String] == ids.map(\.uuidString))
    }

    @Test func patchSendsOnlySetKeys() throws {
        #expect(try snakeCaseKeys(ReimbursementPatch()).isEmpty)
        let submit = try snakeCaseKeys(ReimbursementPatch(status: .submitted, submittedDate: .some(oct19)))
        #expect(submit as NSDictionary == ["status": "submitted", "submitted_date": "2026-10-19"] as NSDictionary)

        let entry = UUID()
        let paid = try snakeCaseKeys(
            ReimbursementPatch(status: .paid, paidDate: .some(oct19), incomeEntryId: .some(entry))
        )
        #expect(Set(paid.keys) == ["status", "paid_date", "income_entry_id"])
        #expect(paid["paid_date"] as? String == "2026-10-19")
        #expect(paid["income_entry_id"] as? String == entry.uuidString)

        let cleared = try snakeCaseKeys(ReimbursementPatch(note: .some(nil)))
        #expect(cleared as NSDictionary == ["note": NSNull()] as NSDictionary)

        let lists = try snakeCaseKeys(ReimbursementPatch(addTransactionIds: [entry]))
        #expect(Set(lists.keys) == ["add_transaction_ids"])
        let removal = try snakeCaseKeys(ReimbursementPatch(removeTransactionIds: [entry]))
        #expect(Set(removal.keys) == ["remove_transaction_ids"])
    }

    @Test func patchShowsItsFieldsOnTheReportAndKeepsTheTotal() throws {
        let reports = try JSONDecoder.api.decode(ReimbursementPage.self, from: jsonFixture("reimbursements")).items
        let report = try #require(reports.dropFirst().first)
        let shown = report.applying(
            ReimbursementPatch(status: .paid, paidDate: .some(oct19), addTransactionIds: [UUID()])
        )
        #expect(shown.status == .paid)
        #expect(shown.paidDate == oct19)
        #expect(shown.submittedDate == report.submittedDate)
        #expect(shown.total == report.total)
        #expect(shown.transactionCount == 1)
    }

    // MARK: Endpoints

    @Test func reimbursementEndpointsSendTypedRequests() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        var requests = reply(200, body: Data(ReimbursementJSON.page().utf8)).makeAsyncIterator()
        _ = try await api.reimbursements()
        _ = try await api.reimbursements(status: .submitted)
        let all = try #require(await requests.next())
        let submitted = try #require(await requests.next())
        #expect(all.url?.path() == "/api/reimbursements")
        #expect(queryItems(all).isEmpty)
        #expect(queryItems(submitted) == [URLQueryItem(name: "status", value: "submitted")])

        var owedRequests = reply(200, body: try jsonFixture("owed")).makeAsyncIterator()
        let owed = try await api.owedToYou()
        let owedRequest = try #require(await owedRequests.next())
        #expect(owedRequest.url?.path() == "/api/reimbursements/owed")
        #expect(owed.owed == money("120"))
    }

    @Test func transactionQuerySendsTheReportAndSplitFilters() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let report = UUID()
        var requests = reply(200, body: Data(#"{"items":[],"next_cursor":null}"#.utf8)).makeAsyncIterator()
        _ = try await api.transactions(TransactionQuery(reimbursementId: report, excludeSplitParents: true, limit: 500))
        let request = try #require(await requests.next())
        #expect(Set(queryItems(request)) == [
            URLQueryItem(name: "reimbursement_id", value: uuidString(report)),
            URLQueryItem(name: "exclude_split_parents", value: "true"),
            URLQueryItem(name: "limit", value: "500"),
        ])
    }

    @Test func incomeWithoutAMonthAsksForEveryMonth() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let empty = #"{"items":[],"totals":{"expected":"0.00","received":"0.00"}}"#
        var requests = reply(200, body: Data(empty.utf8)).makeAsyncIterator()
        _ = try await api.income(type: .reimbursement)
        let request = try #require(await requests.next())
        #expect(queryItems(request) == [URLQueryItem(name: "type", value: "reimbursement")])
    }

    @Test func createAndUpdateEndpointsSendThePhoneIdAndTheSetKeys() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let id = UUID()
        var requests = reply(200, body: Data(ReimbursementJSON.report(id, status: "draft").utf8)).makeAsyncIterator()
        _ = try await api.createReimbursement(ReimbursementDraft(id: id, note: "Trip"))
        _ = try await api.updateReimbursement(id: id, patch: ReimbursementPatch(note: .some("Trip 2")))
        let post = try #require(await requests.next())
        let patch = try #require(await requests.next())
        #expect(post.httpMethod == "POST")
        #expect(post.url?.path() == "/api/reimbursements")
        #expect(Set(try jsonBody(post).keys) == ["id", "note"])
        #expect(patch.httpMethod == "PATCH")
        #expect(patch.url?.path() == "/api/reimbursements/\(uuidString(id))")
        #expect(try jsonBody(patch) as NSDictionary == ["note": "Trip 2"] as NSDictionary)
    }

    // MARK: Loading

    @Test func loadShowsOwedUnsubmittedExpensesAndGroupedReports() async throws {
        defer { try? fixture.cleanUp() }
        let free = UUID()
        let (model, _, server) = await loadedReimbursementsModel()
        let api = makeClient(fixture.make())
        server.update {
            $0.transactions = """
            {"items":[\(ReimbursementJSON.expense(UUID(), report: ReimbursementJSON.submittedId)),\
            \(ReimbursementJSON.expense(free)),\(ReimbursementJSON.expense(UUID(), pending: true)),\
            \(ReimbursementJSON.expense(UUID(), report: ReimbursementJSON.paidId))],"next_cursor":null}
            """
        }
        await model.load(api: api)

        #expect(model.owed == money("120.00"))
        #expect(model.unsubmitted.map(\.id) == [free])
        #expect(model.openReports.map(\.id) == [ReimbursementJSON.draftId, ReimbursementJSON.submittedId])
        #expect(model.paidReports.map(\.id) == [ReimbursementJSON.paidId])
        #expect(model.reportsAvailableToAdd.map(\.id) == [ReimbursementJSON.draftId])
        #expect(model.hasData)

        let query = try #require(server.requests("GET", "/api/transactions").last)
        #expect(Set(queryItems(query)) == [
            URLQueryItem(name: "bucket", value: "reimbursable"),
            URLQueryItem(name: "exclude_split_parents", value: "true"),
            URLQueryItem(name: "limit", value: "500"),
        ])
    }

    @Test func openReportsListDraftsFirstAndPaidOnesMostRecentFirst() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api, server) = await loadedReimbursementsModel()
        let early = UUID(), late = UUID(), newDraft = UUID()
        server.update {
            $0.reports = ReimbursementJSON.page(
                ReimbursementJSON.report(ReimbursementJSON.submittedId, status: "submitted", submitted: "2026-10-10"),
                ReimbursementJSON.report(newDraft, status: "draft"),
                ReimbursementJSON.report(
                    early, status: "paid", submitted: "2026-08-01", paid: "2026-08-05", income: UUID()
                ),
                ReimbursementJSON.report(
                    late, status: "paid", submitted: "2026-09-01", paid: "2026-09-05", income: UUID()
                )
            )
        }
        await model.load(api: api)
        #expect(model.openReports.map(\.id) == [newDraft, ReimbursementJSON.submittedId])
        #expect(model.paidReports.map(\.id) == [late, early])
    }

    @Test func failedRefreshKeepsEverythingAndSetsTheError() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api, server) = await loadedReimbursementsModel()
        server.update { $0.failReads = true }
        await model.load(api: api)

        #expect(model.error?.errorDescription == "Boom.")
        #expect(model.hasData)
        #expect(model.owed == money("120.00"))
        #expect(model.openReports.count == 2)
    }

    @Test func settlingIncomeOffersOnlyEntriesNoReportUses() async throws {
        defer { try? fixture.cleanUp() }
        let linked = UUID(), free = UUID()
        let server = FakeReimbursementServer()
        server.update {
            $0.reports = ReimbursementJSON.page(ReimbursementJSON.report(
                ReimbursementJSON.paidId, status: "paid", submitted: "2026-09-25", paid: "2026-10-02", income: linked
            ))
        }
        let income = #"{"items":[\#(ReimbursementJSON.incomeEntry(linked)),\#(ReimbursementJSON.incomeEntry(free))],"#
            + #""totals":{"expected":"0.00","received":"120.00"}}"#
        server.update { $0.income = income }
        let api = makeClient(fixture.make())
        let model = ReimbursementsModel()
        await model.load(api: api)

        let offered = try await model.incomeAvailableToSettle(api: api)
        #expect(offered.map(\.id) == [free])
        let request = try #require(server.requests("GET", "/api/income").first)
        #expect(queryItems(request) == [URLQueryItem(name: "type", value: "reimbursement")])
    }

    // MARK: Writes

    @Test func createdReportSendsThePhoneIdAndRefreshesWhatIsOwed() async throws {
        defer { try? fixture.cleanUp() }
        let id = UUID()
        let taken = UUID()
        let (model, api, server) = await loadedReimbursementsModel { request, server in
            server.update {
                $0.reports = ReimbursementJSON.page(
                    ReimbursementJSON.report(id, status: "draft", note: "Trip", total: "35.00", count: 1)
                )
                $0.owed = #"{"owed":"35.00"}"#
            }
            return (201, ReimbursementJSON.report(id, status: "draft", note: "Trip", total: "35.00", count: 1))
        }

        let created = await model.createReport(id: id, note: " Trip ", transactionIds: [taken], api: api)
        #expect(created)
        #expect(model.createError == nil)
        let post = try #require(server.requests("POST", "/api/reimbursements").first)
        let body = try jsonBody(post)
        #expect(Set(body.keys) == ["id", "note", "transaction_ids"])
        #expect(body["id"] as? String == id.uuidString)
        #expect(body["note"] as? String == "Trip")
        #expect(model.owed == money("35.00"))
        #expect(model.openReports.map(\.id) == [id])
    }

    @Test func failedCreateKeepsTheReportsAndSetsCreateError() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api, server) = await loadedReimbursementsModel { _, _ in
            (409, ReimbursementJSON.envelope("This transaction is already in another report."))
        }
        let created = await model.createReport(id: UUID(), note: nil, transactionIds: [UUID()], api: api)
        #expect(!created)
        #expect(model.createError?.errorDescription == "This transaction is already in another report.")
        #expect(model.openReports.count == 2)
        let post = try #require(server.requests("POST", "/api/reimbursements").first)
        #expect(Set(try jsonBody(post).keys) == ["id", "transaction_ids"])
    }

    @Test func submitShowsAtOnceThenRollsBackOnA422() async throws {
        defer { try? fixture.cleanUp() }
        let (arrived, arrivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let (model, api, server) = await loadedReimbursementsModel { _, _ in
            arrivedSignal.yield()
            for await _ in release { break }
            let message = "submitted_date is required once a report is submitted"
            return (422, #"{"error":{"code":"validation_error","message":"\#(message)","request_id":"r3"}}"#)
        }
        let draft = try #require(model.openReports.first)
        #expect(draft.id == ReimbursementJSON.draftId)

        let edit = Task { await model.submit(draft, on: oct19, api: api) }
        for await _ in arrived { break }
        #expect(model.openReports.map(\.status) == [.submitted, .submitted])
        #expect(model.reportsAvailableToAdd.isEmpty)
        releaseSignal.yield()
        let saved = await edit.value

        #expect(!saved)
        #expect(model.openReports.map(\.status) == [.draft, .submitted])
        #expect(model.stepError?.error.errorDescription == "submitted_date is required once a report is submitted")
        #expect(model.stepError?.rowName == "Pit crew tools and fuel")
        #expect(model.editError == nil)
        #expect(server.requests("GET", "/api/reimbursements").count == 1)
        let path = "/api/reimbursements/\(uuidString(ReimbursementJSON.draftId))"
        let patch = try #require(server.requests("PATCH", path).first)
        let expected: NSDictionary = ["status": "submitted", "submitted_date": "2026-10-19"]
        #expect(try jsonBody(patch) as NSDictionary == expected)
    }

    @Test func savedSubmitLoadsTheServersReportsAndOwedAgain() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api, server) = await loadedReimbursementsModel { _, server in
            let submitted = ReimbursementJSON.report(
                ReimbursementJSON.draftId, status: "submitted", note: "Pit crew tools and fuel",
                submitted: "2026-10-19", total: "35.00", count: 1
            )
            server.update {
                $0.reports = ReimbursementJSON.page(submitted)
                $0.owed = #"{"owed":"35.00"}"#
            }
            return (200, submitted)
        }
        let draft = try #require(model.openReports.first)
        let saved = await model.submit(draft, on: oct19, api: api)

        #expect(saved)
        #expect(model.stepError == nil)
        #expect(model.editError == nil)
        #expect(model.openReports.first?.submittedDate == oct19)
        #expect(model.openReports.first?.total == money("35.00"))
        #expect(model.owed == money("35.00"))
        #expect(server.requests("GET", "/api/reimbursements/owed").count == 2)
    }

    @Test func markPaidSendsTheIncomeEntryAndSurfacesAConflict() async throws {
        defer { try? fixture.cleanUp() }
        let entry = UUID()
        let message = "This income entry is already linked to another report."
        let (model, api, server) = await loadedReimbursementsModel { _, _ in
            (409, ReimbursementJSON.envelope(message))
        }
        let submitted = try #require(model.openReports.last)
        #expect(submitted.id == ReimbursementJSON.submittedId)

        let saved = await model.markPaid(submitted, on: oct19, incomeEntryId: entry, api: api)

        #expect(!saved)
        #expect(model.stepError?.error.errorDescription == message)
        #expect(model.editError == nil)
        #expect(model.openReports.last?.status == .submitted)
        #expect(model.paidReports.map(\.id) == [ReimbursementJSON.paidId])
        let path = "/api/reimbursements/\(uuidString(ReimbursementJSON.submittedId))"
        let patch = try #require(server.requests("PATCH", path).first)
        let body = try jsonBody(patch)
        #expect(Set(body.keys) == ["status", "paid_date", "income_entry_id"])
        #expect(body["status"] as? String == "paid")
        #expect(body["paid_date"] as? String == "2026-10-19")
        #expect(body["income_entry_id"] as? String == entry.uuidString)
    }

    @Test func modelRefusesABackwardOrSkippingStep() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api, server) = await loadedReimbursementsModel { _, _ in (200, "{}") }
        let draft = try #require(model.openReports.first)
        let submitted = try #require(model.openReports.last)
        let paid = try #require(model.paidReports.first)

        #expect(await !model.markPaid(draft, on: oct19, incomeEntryId: UUID(), api: api))
        #expect(await !model.submit(submitted, on: oct19, api: api))
        #expect(await !model.submit(paid, on: oct19, api: api))
        #expect(await !model.markPaid(paid, on: oct19, incomeEntryId: UUID(), api: api))
        #expect(server.requests.filter { $0.httpMethod != "GET" }.isEmpty)
        #expect(model.openReports.map(\.status) == [.draft, .submitted])
        #expect(model.editError == nil)
        #expect(model.stepError == nil)
    }

    @Test func expensesCanOnlyBeAddedToOrRemovedFromADraft() async throws {
        defer { try? fixture.cleanUp() }
        let tx = UUID()
        let (model, api, server) = await loadedReimbursementsModel { _, server in
            let saved = ReimbursementJSON.report(
                ReimbursementJSON.draftId, status: "draft", note: "Pit crew tools and fuel", total: "35.00", count: 1
            )
            server.update { $0.reports = ReimbursementJSON.page(saved) }
            return (200, saved)
        }
        let draft = try #require(model.openReports.first)
        let submitted = try #require(model.openReports.last)
        let paid = try #require(model.paidReports.first)

        #expect(await !model.add([tx], to: submitted, api: api))
        #expect(await !model.remove([tx], from: paid, api: api))
        #expect(await !model.add([], to: draft, api: api))
        #expect(server.requests.filter { $0.httpMethod != "GET" }.isEmpty)

        #expect(await model.add([tx], to: draft, api: api))
        #expect(await model.remove([tx], from: draft, api: api))
        let patches = server.requests("PATCH", "/api/reimbursements/\(uuidString(ReimbursementJSON.draftId))")
        #expect(try patches.map { Set(try jsonBody($0).keys) } == [["add_transaction_ids"], ["remove_transaction_ids"]])
        #expect(try jsonBody(patches[0])["add_transaction_ids"] as? [String] == [tx.uuidString])
        #expect(model.openReports.first?.total == money("35.00"))
    }

    @Test func failedAddShowsAsTheScreensAlertNotAStepError() async throws {
        defer { try? fixture.cleanUp() }
        let message = "This transaction is already in another report."
        let (model, api, _) = await loadedReimbursementsModel { _, _ in (409, ReimbursementJSON.envelope(message)) }
        let draft = try #require(model.openReports.first)

        #expect(await !model.add([UUID()], to: draft, api: api))
        #expect(model.editError?.error.errorDescription == message)
        #expect(model.editError?.rowName == "Pit crew tools and fuel")
        #expect(model.stepError == nil)
    }

    @Test func markedPaidReportShowsWhoPaidItWithoutARefresh() async throws {
        defer { try? fixture.cleanUp() }
        let entry = UUID()
        let (model, api, _) = await loadedReimbursementsModel { _, server in
            let paid = ReimbursementJSON.report(
                ReimbursementJSON.submittedId, status: "paid", note: "October race weekend",
                submitted: "2026-10-10", paid: "2026-10-19", income: entry, total: "85.00", count: 1
            )
            let income = #"{"items":[\#(ReimbursementJSON.incomeEntry(entry))],"#
                + #""totals":{"expected":"0.00","received":"60.00"}}"#
            server.update {
                $0.reports = ReimbursementJSON.page(paid)
                $0.income = income
            }
            return (200, paid)
        }
        let submitted = try #require(model.report(id: ReimbursementJSON.submittedId))
        #expect(model.settledBy(submitted) == nil)

        #expect(await model.markPaid(submitted, on: oct19, incomeEntryId: entry, api: api))
        let paid = try #require(model.report(id: ReimbursementJSON.submittedId))
        #expect(model.settledBy(paid)?.id == entry)
    }

    // MARK: Paging

    @Test func unsubmittedExpensesComeFromEveryPage() async throws {
        defer { try? fixture.cleanUp() }
        let first = UUID(), second = UUID()
        let server = FakeReimbursementServer()
        server.update {
            $0.transactions = """
            {"items":[\(ReimbursementJSON.expense(first)),\
            \(ReimbursementJSON.expense(UUID(), report: ReimbursementJSON.submittedId))],"next_cursor":"page-2"}
            """
            $0.laterTransactionPages["page-2"] = """
            {"items":[\(ReimbursementJSON.expense(UUID(), pending: true)),\(ReimbursementJSON.expense(second))],\
            "next_cursor":null}
            """
        }
        let api = makeClient(fixture.make())
        let model = ReimbursementsModel()
        await model.load(api: api)

        #expect(model.unsubmitted.map(\.id) == [first, second])
        let requests = server.requests("GET", "/api/transactions")
        try #require(requests.count == 2)
        #expect(!queryItems(requests[0]).contains { $0.name == "cursor" })
        #expect(Set(queryItems(requests[1])) == [
            URLQueryItem(name: "bucket", value: "reimbursable"),
            URLQueryItem(name: "exclude_split_parents", value: "true"),
            URLQueryItem(name: "limit", value: "500"),
            URLQueryItem(name: "cursor", value: "page-2"),
        ])
    }

    @Test func reportExpensesComeFromEveryPage() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api, server) = await loadedReimbursementsModel()
        let first = UUID(), second = UUID()
        let report = ReimbursementJSON.submittedId
        server.update {
            $0.transactions = #"{"items":[\#(ReimbursementJSON.expense(first, report: report))],"next_cursor":"next"}"#
            $0.laterTransactionPages["next"] =
                #"{"items":[\#(ReimbursementJSON.expense(second, report: report))],"next_cursor":null}"#
        }
        let submitted = try #require(model.report(id: report))

        let expenses = try await model.expenses(of: submitted, api: api)
        #expect(expenses.map(\.id) == [first, second])
        let last = try #require(server.requests("GET", "/api/transactions").last)
        #expect(Set(queryItems(last)) == [
            URLQueryItem(name: "reimbursement_id", value: uuidString(report)),
            URLQueryItem(name: "limit", value: "500"),
            URLQueryItem(name: "cursor", value: "next"),
        ])
    }

    @Test func allTransactionsStopsAtThePageCap() async throws {
        defer { try? fixture.cleanUp() }
        let server = FakeReimbursementServer()
        server.update {
            $0.transactions = #"{"items":[\#(ReimbursementJSON.expense(UUID()))],"next_cursor":"more"}"#
            $0.laterTransactionPages["more"] = $0.transactions
        }
        let api = makeClient(fixture.make())

        let rows = try await api.allTransactions(TransactionQuery(bucket: .reimbursable), maxPages: 3)
        #expect(rows.count == 3)
        #expect(server.requests("GET", "/api/transactions").count == 3)
    }
}
