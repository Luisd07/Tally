import Foundation
import Testing
@testable import Tally

/// The selection rules and the multi-row edit on `EditableRows`: no network, so they run in parallel.
@MainActor
struct BulkSelectionTests {
    private func transaction(
        id: UUID = UUID(),
        bucket: Bucket = .personal,
        reviewed: Bool = false,
        parentId: UUID? = nil,
        isSplit: Bool = false
    ) -> Transaction {
        Transaction(
            id: id,
            accountId: UUID(),
            postedDate: CalendarDate(year: 2026, month: 10, day: 8),
            transactedAt: nil,
            amount: Money(Decimal(string: "-12.00")!),
            descriptionRaw: "CORNER CAFE",
            merchant: nil,
            categoryId: nil,
            bucket: bucket,
            source: .manual,
            externalId: nil,
            pending: false,
            reviewed: reviewed,
            notes: nil,
            eventId: nil,
            parentId: parentId,
            reimbursementId: nil,
            isSplit: isSplit,
            createdAt: .now,
            updatedAt: .now
        )
    }

    // MARK: Selection

    @Test func selectDeselectToggleAndClear() {
        let model = TransactionsModel()
        let ids = (0..<3).map { _ in UUID() }
        ids.forEach(model.select)
        model.deselect(ids[0])
        model.toggleSelection(ids[1])
        model.toggleSelection(ids[0])
        #expect(model.selection == [ids[0], ids[2]])
        #expect(model.isInSelection(ids[2]))
        model.clearSelection()
        #expect(model.selection.isEmpty)
    }

    @Test func selectionStopsAtTheBulkLimit() {
        let model = TransactionsModel()
        for _ in 0..<TransactionBulkUpdate.maxIds { model.select(UUID()) }
        #expect(model.isSelectionFull)

        let extra = UUID()
        model.select(extra)
        model.toggleSelection(extra)
        #expect(model.selection.count == TransactionBulkUpdate.maxIds)
        #expect(!model.isInSelection(extra))
    }

    // MARK: Multi-row edit

    /// Starts a multi-row edit of `ids` whose send waits for `release`, and returns once it was sent.
    private func heldEditMany(
        on rows: EditableRows<Transaction>,
        ids: [UUID],
        patch: TransactionPatch = TransactionPatch(reviewed: true),
        leaves: @escaping (Transaction) -> Bool = { _ in false },
        reply: @escaping ([UUID]) throws -> [Transaction]
    ) async -> (task: Task<Bool, Never>, release: AsyncStream<Void>.Continuation) {
        let (sent, signalSent) = AsyncStream.makeStream(of: Void.self)
        let (released, release) = AsyncStream.makeStream(of: Void.self)
        let task = Task {
            await rows.editMany(ids: ids, RowEdit(rowName: "Rows", patch: patch, leaves: leaves)) { ids in
                signalSent.yield()
                for await _ in released { break }
                return try reply(ids)
            }
        }
        for await _ in sent { break }
        return (task, release)
    }

    /// Starts a single edit of row `id` whose send waits for `release`, and returns once it was sent.
    private func heldEdit(
        on rows: EditableRows<Transaction>,
        id: UUID,
        patch: TransactionPatch,
        reply: @escaping () throws -> Transaction
    ) async -> (task: Task<Bool, Never>, release: AsyncStream<Void>.Continuation) {
        let (sent, signalSent) = AsyncStream.makeStream(of: Void.self)
        let (released, release) = AsyncStream.makeStream(of: Void.self)
        let task = Task {
            await rows.edit(id: id, RowEdit(rowName: "Row", patch: patch, leaves: { _ in false })) {
                signalSent.yield()
                for await _ in released { break }
                return try reply()
            }
        }
        for await _ in sent { break }
        return (task, release)
    }

    @Test func editManySendsOnlyListedIdsInListOrder() async {
        let listed = [transaction(), transaction(), transaction()]
        let rows = EditableRows<Transaction>()
        rows.replace(with: listed)
        var sent: [UUID] = []
        let saved = await rows.editMany(
            ids: [listed[2].id, UUID(), listed[0].id],
            RowEdit(rowName: "Rows", patch: TransactionPatch(reviewed: true), leaves: { _ in false })
        ) { ids in
            sent = ids
            return ids.map { id in listed.first { $0.id == id }!.applying(TransactionPatch(reviewed: true)) }
        }
        #expect(saved)
        #expect(sent == [listed[0].id, listed[2].id])
        #expect(rows.items.map(\.reviewed) == [true, false, true])
    }

    @Test func editManyOfNoListedRowSendsNothing() async {
        let rows = EditableRows<Transaction>()
        rows.replace(with: [transaction()])
        var sent = false
        let saved = await rows.editMany(
            ids: [UUID()], RowEdit(rowName: "Rows", patch: TransactionPatch(reviewed: true), leaves: { _ in false })
        ) { _ in
            sent = true
            return []
        }
        #expect(!saved)
        #expect(!sent)
    }

    @Test func failedEditManyPutsRemovedRowsBackInPlace() async {
        let listed = (0..<5).map { transaction(bucket: $0.isMultiple(of: 2) ? .business : .personal) }
        let rows = EditableRows<Transaction>()
        rows.replace(with: listed)
        var countChanges: [Int] = []
        let saved = await rows.editMany(
            ids: listed.map(\.id),
            RowEdit(rowName: "5 transactions", patch: TransactionPatch(bucket: .personal), leaves: { $0.bucket == .business }),
            changed: { countChanges.append($0) }
        ) { _ in
            #expect(rows.items.map(\.id) == [listed[1].id, listed[3].id])
            throw URLError(.badServerResponse)
        }
        #expect(!saved)
        #expect(rows.items == listed)
        #expect(countChanges == [-3, 3])
        #expect(rows.editError?.rowName == "5 transactions")
    }

    @Test func failedEditManyKeepsASingleEditStillInFlight() async {
        let listed = [transaction(), transaction()]
        let rows = EditableRows<Transaction>()
        rows.replace(with: listed)
        let notes = TransactionPatch(notes: .some("Lunch"))
        let single = await heldEdit(on: rows, id: listed[0].id, patch: notes) { listed[0].applying(notes) }
        let bulk = await heldEditMany(on: rows, ids: listed.map(\.id)) { _ in throw URLError(.badServerResponse) }
        #expect(rows.items.map(\.reviewed) == [true, true])

        bulk.release.yield()
        #expect(await bulk.task.value == false)
        #expect(rows.items[0].notes == "Lunch")
        #expect(rows.items.map(\.reviewed) == [false, false])

        single.release.yield()
        #expect(await single.task.value)
        #expect(rows.items[0] == listed[0].applying(notes))
    }

    @Test func failedSingleEditKeepsAnEditManyStillInFlight() async {
        let listed = [transaction(), transaction()]
        let rows = EditableRows<Transaction>()
        rows.replace(with: listed)
        let notes = TransactionPatch(notes: .some("Lunch"))
        let bulk = await heldEditMany(on: rows, ids: listed.map(\.id)) { ids in
            ids.map { id in listed.first { $0.id == id }!.applying(TransactionPatch(reviewed: true)) }
        }
        let single = await heldEdit(on: rows, id: listed[1].id, patch: notes) { throw URLError(.badServerResponse) }
        single.release.yield()
        #expect(await single.task.value == false)
        #expect(rows.items[1].reviewed)
        #expect(rows.items[1].notes == nil)

        bulk.release.yield()
        #expect(await bulk.task.value)
        #expect(rows.items.map(\.reviewed) == [true, true])
    }

    @Test func editManyLeavesASplicedRowAloneAndSettlesTheOthers() async {
        let listed = [transaction(), transaction()]
        let rows = EditableRows<Transaction>()
        rows.replace(with: listed)
        let bulk = await heldEditMany(on: rows, ids: listed.map(\.id)) { _ in throw URLError(.badServerResponse) }

        let split = transaction(id: listed[0].id, isSplit: true)
        rows.splice(parent: split, children: [], removing: { $0.parentId == split.id })
        bulk.release.yield()
        #expect(await bulk.task.value == false)
        #expect(rows.items == [split, listed[1]])
    }

    @Test func landingEditManyDoesNotOverwriteASplicedRow() async {
        let listed = [transaction(), transaction()]
        let rows = EditableRows<Transaction>()
        rows.replace(with: listed)
        let bulk = await heldEditMany(on: rows, ids: listed.map(\.id)) { _ in
            listed.map { $0.applying(TransactionPatch(reviewed: true)) }
        }

        let split = transaction(id: listed[0].id, isSplit: true)
        rows.splice(parent: split, children: [], removing: { $0.parentId == split.id })
        bulk.release.yield()
        #expect(await bulk.task.value)
        #expect(rows.items[0] == split)
        #expect(rows.items[1].reviewed)
    }

    @Test func failedEditManyAfterAReloadLeavesTheNewRowsAlone() async {
        let listed = [transaction(), transaction()]
        let rows = EditableRows<Transaction>()
        rows.replace(with: listed)
        let bulk = await heldEditMany(on: rows, ids: listed.map(\.id), leaves: { _ in true }) { _ in
            throw URLError(.badServerResponse)
        }
        #expect(rows.items.isEmpty)

        let reloaded = [transaction()]
        rows.replace(with: reloaded)
        bulk.release.yield()
        #expect(await bulk.task.value == false)
        #expect(rows.items == reloaded)
        #expect(rows.editError != nil)
    }

    @Test func editManyReplyMissingARowFailsAndPutsEveryRowBack() async {
        let listed = (0..<4).map { transaction(bucket: $0.isMultiple(of: 2) ? .business : .personal) }
        let rows = EditableRows<Transaction>()
        rows.replace(with: listed)
        var countChanges: [Int] = []
        let patch = TransactionPatch(bucket: .personal)
        let saved = await rows.editMany(
            ids: listed.map(\.id),
            RowEdit(rowName: "4 transactions", patch: patch, leaves: { $0.bucket == .business }),
            changed: { countChanges.append($0) }
        ) { ids in
            ids.dropFirst().map { id in listed.first { $0.id == id }!.applying(patch) }
        }
        #expect(!saved)
        #expect(rows.items == listed)
        #expect(countChanges == [-2, 2])
        #expect(rows.editError?.rowName == "4 transactions")
    }
}

/// Lives in `APIClientTests`' serialized suite because the network tests share `StubURLProtocol`'s one handler.
extension APIClientTests {
    private func bulkRowJSON(
        id: UUID,
        bucket: String = "personal",
        categoryId: UUID? = nil,
        reviewed: Bool = false,
        updatedAt: String = "2026-10-18T20:42:06.089964Z"
    ) -> String {
        let category = categoryId.map { "\"\(uuidString($0))\"" } ?? "null"
        return """
        {"id":"\(uuidString(id))","account_id":"265cd02e-4a77-51ea-ad3a-4a9fbc282065","posted_date":"2026-10-08","transacted_at":null,"amount":"-85.00","description_raw":"HERTZ","merchant":null,"category_id":\(category),"bucket":"\(bucket)","source":"manual","external_id":null,"pending":false,"reviewed":\(reviewed),"notes":null,"event_id":null,"parent_id":null,"reimbursement_id":null,"is_split":false,"created_at":"2026-10-18T20:42:06.089964Z","updated_at":"\(updatedAt)"}
        """
    }

    private func bulkPageJSON(_ rows: [String]) -> Data {
        Data(#"{"items":[\#(rows.joined(separator: ","))],"next_cursor":null}"#.utf8)
    }

    private func bulkResultJSON(_ rows: [String]) -> Data {
        Data(#"{"updated":\#(rows.count),"items":[\#(rows.joined(separator: ","))]}"#.utf8)
    }

    private var notFoundEnvelope: Data {
        Data(#"{"error":{"code":"not_found","message":"Not found.","request_id":"r404"}}"#.utf8)
    }

    /// A model loaded with one row per id, each in `bucket`, under `filterBucket`'s filter.
    private func bulkListModel(
        _ ids: [UUID],
        bucket: String = "personal",
        filterBucket: Bucket? = nil,
        onEdited: (() -> Void)? = nil
    ) async -> (TransactionsModel, APIClient) {
        let api = makeClient(fixture.make())
        let model = TransactionsModel(onEdited: onEdited)
        model.filter.bucket = filterBucket
        _ = reply(200, body: bulkPageJSON(ids.map { bulkRowJSON(id: $0, bucket: bucket) }))
        await model.load(api: api)
        return (model, api)
    }

    private func jsonBody(_ request: URLRequest) throws -> [String: Any] {
        let body = try #require(bodyData(request))
        return try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
    }

    // MARK: Models

    @Test func bulkUpdateEncodesIdsAndOnlyTheSetPatchKeys() throws {
        let ids = [UUID(), UUID()]
        let category = UUID()
        let json = try snakeCaseKeys(TransactionBulkUpdate(ids: ids, patch: TransactionPatch(categoryId: .some(category))))
        #expect(Set(json.keys) == ["ids", "patch"])
        #expect((json["ids"] as? [String])?.map { $0.lowercased() } == ids.map(uuidString))
        let patch = try #require(json["patch"] as? [String: Any])
        #expect(Set(patch.keys) == ["category_id"])
        #expect((patch["category_id"] as? String)?.lowercased() == uuidString(category))

        let cleared = try snakeCaseKeys(TransactionBulkUpdate(ids: ids, patch: TransactionPatch(categoryId: .some(nil))))
        let clearedPatch = try #require(cleared["patch"] as? [String: Any])
        #expect(Set(clearedPatch.keys) == ["category_id"])
        #expect(clearedPatch["category_id"] is NSNull)
    }

    @Test func bulkResultDecodes() throws {
        let id = UUID()
        let result = try JSONDecoder.api.decode(BulkResult.self, from: bulkResultJSON([bulkRowJSON(id: id, reviewed: true)]))
        #expect(result.updated == 1)
        #expect(result.items.map(\.id) == [id])
        #expect(result.items[0].reviewed)
    }

    // MARK: Endpoint and model

    @Test func bulkSetCategoryPostsTheSelectedIdsWithOnlyTheCategory() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<3).map { _ in UUID() }
        let (model, api) = await bulkListModel(ids)
        let category = UUID()
        model.select(ids[2])
        model.select(ids[0])

        let saved = [ids[0], ids[2]].map { bulkRowJSON(id: $0, categoryId: category) }
        var requests = reply(200, body: bulkResultJSON(saved)).makeAsyncIterator()
        await model.bulkSetCategory(category, api: api)
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "POST")
        #expect(request.url?.path() == "/api/transactions/bulk")
        let json = try jsonBody(request)
        #expect(Set(json.keys) == ["ids", "patch"])
        #expect((json["ids"] as? [String])?.map { $0.lowercased() } == [ids[0], ids[2]].map(uuidString))
        let patch = try #require(json["patch"] as? [String: Any])
        #expect(Set(patch.keys) == ["category_id"])
        #expect(model.items.map(\.categoryId) == [category, nil, category])
    }

    @Test func bulkClearingTheCategorySendsNull() async throws {
        defer { try? fixture.cleanUp() }
        let ids = [UUID()]
        let (model, api) = await bulkListModel(ids)
        model.selectAllLoaded()

        var requests = reply(200, body: bulkResultJSON([bulkRowJSON(id: ids[0])])).makeAsyncIterator()
        await model.bulkSetCategory(nil, api: api)
        let request = try #require(await requests.next())
        let patch = try #require(try jsonBody(request)["patch"] as? [String: Any])
        #expect(Set(patch.keys) == ["category_id"])
        #expect(patch["category_id"] is NSNull)
    }

    @Test func bulkEditShowsOnEverySelectedRowBeforeTheServerAnswers() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<3).map { _ in UUID() }
        let (model, api) = await bulkListModel(ids)
        model.selectAllLoaded()
        let (arrived, arrivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let result = bulkResultJSON(ids.map { bulkRowJSON(id: $0, reviewed: true) })
        StubURLProtocol.respond { request in
            arrivedSignal.yield()
            for await _ in release { break }
            return (StubURLProtocol.response(to: request, status: 200), result)
        }

        let editing = Task { await model.bulkSetReviewed(true, api: api) }
        for await _ in arrived { break }
        #expect(model.items.allSatisfy { $0.reviewed })
        #expect(model.selection.count == 3)

        releaseSignal.yield()
        await editing.value
        #expect(model.items.allSatisfy { $0.reviewed })
        #expect(model.selection.isEmpty)
    }

    @Test func bulkNotFoundRollsEveryRowBackAndKeepsTheSelection() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<3).map { _ in UUID() }
        var edits = 0
        let (model, api) = await bulkListModel(ids, onEdited: { edits += 1 })
        let before = model.items
        model.selectAllLoaded()

        _ = reply(404, body: notFoundEnvelope)
        await model.bulkSetBucket(.business, api: api)
        #expect(model.items == before)
        #expect(model.editError?.error.errorDescription == "Not found.")
        #expect(model.editError?.message == "3 transactions: Not found.")
        #expect(model.selection == Set(ids))
        #expect(edits == 0)
    }

    @Test func bulkSuccessCommitsTheServerRowsAndClearsTheSelection() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<2).map { _ in UUID() }
        var edits = 0
        let (model, api) = await bulkListModel(ids, onEdited: { edits += 1 })
        let loadedAt = model.items[0].updatedAt
        model.selectAllLoaded()
        model.beginSelecting()

        let later = "2026-10-19T09:00:00Z"
        _ = reply(200, body: bulkResultJSON(ids.map { bulkRowJSON(id: $0, reviewed: true, updatedAt: later) }))
        await model.bulkSetReviewed(true, api: api)
        #expect(model.items.map(\.id) == ids)
        #expect(model.items.allSatisfy { $0.reviewed })
        #expect(model.items.allSatisfy { $0.updatedAt != loadedAt })
        #expect(model.selection.isEmpty)
        #expect(model.isSelecting)
        #expect(model.editError == nil)
        #expect(edits == 1)
    }

    @Test func bulkBucketChangeRemovesRowsTheBucketFilterNoLongerAdmits() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<3).map { _ in UUID() }
        let (model, api) = await bulkListModel(ids, bucket: "business", filterBucket: .business)
        model.select(ids[0])
        model.select(ids[2])

        _ = reply(200, body: bulkResultJSON([ids[0], ids[2]].map { bulkRowJSON(id: $0, bucket: "personal") }))
        await model.bulkSetBucket(.personal, api: api)
        #expect(model.items.map(\.id) == [ids[1]])
        #expect(model.selection.isEmpty)
    }

    @Test func failedBulkBucketChangeUnderAFilterPutsTheRowsBack() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<3).map { _ in UUID() }
        let (model, api) = await bulkListModel(ids, bucket: "business", filterBucket: .business)
        model.select(ids[0])
        model.select(ids[2])

        _ = reply(404, body: notFoundEnvelope)
        await model.bulkSetBucket(.personal, api: api)
        #expect(model.items.map(\.id) == ids)
        #expect(model.items.allSatisfy { $0.bucket == .business })
        #expect(model.selection == [ids[0], ids[2]])
    }

    @Test func selectAllLoadedStopsAtTheBulkLimitAndSendsThatMany() async throws {
        defer { try? fixture.cleanUp() }
        let maxIds = TransactionBulkUpdate.maxIds
        let ids = (0..<maxIds + 1).map { _ in UUID() }
        let (model, api) = await bulkListModel(ids)
        model.selectAllLoaded()
        #expect(model.selection == Set(ids.prefix(maxIds)))
        #expect(model.isSelectionFull)
        #expect(!model.canSelectAll)
        model.select(ids[maxIds])
        #expect(!model.isInSelection(ids[maxIds]))

        var requests = reply(200, body: bulkResultJSON(ids.prefix(maxIds).map { bulkRowJSON(id: $0, reviewed: true) }))
            .makeAsyncIterator()
        await model.bulkSetReviewed(true, api: api)
        let request = try #require(await requests.next())
        let sent = try #require(try jsonBody(request)["ids"] as? [String])
        #expect(sent.count == maxIds)
        #expect(model.selection.isEmpty)
        #expect(!model.items[maxIds].reviewed)
    }

    @Test func filterChangeDropsTheSelection() async throws {
        defer { try? fixture.cleanUp() }
        let ids = [UUID()]
        let (model, api) = await bulkListModel(ids)
        model.selectAllLoaded()

        model.filter.bucket = .business
        _ = reply(200, body: bulkPageJSON([]))
        await model.load(api: api)
        #expect(model.selection.isEmpty)
    }

    @Test func singleEditAfterABulkEditStillPatchesOneRow() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<2).map { _ in UUID() }
        let (model, api) = await bulkListModel(ids)
        model.selectAllLoaded()
        _ = reply(200, body: bulkResultJSON(ids.map { bulkRowJSON(id: $0, reviewed: true) }))
        await model.bulkSetReviewed(true, api: api)

        var requests = reply(200, body: Data(bulkRowJSON(id: ids[1]).utf8)).makeAsyncIterator()
        await model.setReviewed(model.items[1], false, api: api)
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "PATCH")
        #expect(request.url?.path() == "/api/transactions/\(uuidString(ids[1]))")
        #expect(model.items.map(\.reviewed) == [true, false])
    }

    @Test func bulkValidationErrorRollsEveryRowBackWithTheServerMessage() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<2).map { _ in UUID() }
        let (model, api) = await bulkListModel(ids, bucket: "business", filterBucket: .business)
        let before = model.items
        model.selectAllLoaded()

        let envelope = #"{"error":{"code":"validation_error","message":"Unknown category.","request_id":"r422"}}"#
        _ = reply(422, body: Data(envelope.utf8))
        await model.bulkSetCategory(UUID(), api: api)
        #expect(model.items == before)
        #expect(model.editError?.error.status == 422)
        #expect(model.editError?.message == "2 transactions: Unknown category.")
        #expect(model.selection == Set(ids))
    }

    @Test func bulkReplyMissingARowRollsBackAndKeepsTheSelection() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<2).map { _ in UUID() }
        var edits = 0
        let (model, api) = await bulkListModel(ids, onEdited: { edits += 1 })
        let before = model.items
        model.selectAllLoaded()

        _ = reply(200, body: bulkResultJSON([bulkRowJSON(id: ids[0], reviewed: true)]))
        await model.bulkSetReviewed(true, api: api)
        #expect(model.items == before)
        #expect(model.editError != nil)
        #expect(model.selection == Set(ids))
        #expect(edits == 0)
    }

    @Test func reloadKeepsOnlyTheSelectedRowsStillListed() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<3).map { _ in UUID() }
        let (model, api) = await bulkListModel(ids)
        model.selectAllLoaded()

        _ = reply(200, body: bulkPageJSON([ids[0], ids[2]].map { bulkRowJSON(id: $0) }))
        await model.load(api: api)
        #expect(model.selection == [ids[0], ids[2]])

        var requests = reply(200, body: bulkResultJSON([ids[0], ids[2]].map { bulkRowJSON(id: $0, reviewed: true) }))
            .makeAsyncIterator()
        await model.bulkSetReviewed(true, api: api)
        let request = try #require(await requests.next())
        #expect((try jsonBody(request)["ids"] as? [String])?.count == 2)
        #expect(model.selection.isEmpty)
    }

    @Test func reloadWithoutTheSelectedRowsEmptiesTheSelection() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<2).map { _ in UUID() }
        let (model, api) = await bulkListModel(ids)
        model.beginSelecting()
        model.select(ids[0])

        _ = reply(200, body: bulkPageJSON([bulkRowJSON(id: ids[1])]))
        await model.load(api: api)
        #expect(model.selection.isEmpty)
        #expect(model.isSelecting)
        #expect(model.canSelectAll)
    }

    @Test func emptiedListEndsSelecting() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<2).map { _ in UUID() }
        let (model, api) = await bulkListModel(ids, bucket: "business", filterBucket: .business)
        model.beginSelecting()
        model.selectAllLoaded()

        _ = reply(200, body: bulkResultJSON(ids.map { bulkRowJSON(id: $0, bucket: "personal") }))
        await model.bulkSetBucket(.personal, api: api)
        #expect(model.items.isEmpty)
        #expect(model.selection.isEmpty)
        #expect(!model.isSelecting)
    }

    @Test func emptyReloadEndsSelecting() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api) = await bulkListModel([UUID()])
        model.beginSelecting()

        _ = reply(200, body: bulkPageJSON([]))
        await model.load(api: api)
        #expect(!model.isSelecting)
    }

    @Test func canSelectAllUntilEveryListedRowIsSelected() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<2).map { _ in UUID() }
        let (model, _) = await bulkListModel(ids)
        #expect(model.canSelectAll)
        model.select(ids[0])
        #expect(model.canSelectAll)
        model.select(ids[1])
        #expect(!model.canSelectAll)
    }

    @Test func endSelectingStopsPickingAndDropsTheSelection() async throws {
        defer { try? fixture.cleanUp() }
        let (model, _) = await bulkListModel([UUID(), UUID()])
        model.beginSelecting()
        model.selectAllLoaded()

        model.endSelecting()
        #expect(!model.isSelecting)
        #expect(model.selection.isEmpty)
    }

    @Test func sharedCategoryIdIsTheOneCategoryEverySelectedRowHas() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = TransactionsModel()
        let ids = (0..<3).map { _ in UUID() }
        let meals = UUID()
        let rows = [bulkRowJSON(id: ids[0], categoryId: meals), bulkRowJSON(id: ids[1], categoryId: meals)]
        _ = reply(200, body: bulkPageJSON(rows + [bulkRowJSON(id: ids[2])]))
        await model.load(api: api)

        model.select(ids[2])
        #expect(model.sharedCategoryId == nil)
        model.endSelecting()
        model.select(ids[0])
        model.select(ids[1])
        #expect(model.sharedCategoryId == meals)
        model.select(ids[2])
        #expect(model.sharedCategoryId == nil)
    }
}
