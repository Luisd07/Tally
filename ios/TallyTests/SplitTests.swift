import Foundation
import Testing
@testable import Tally

/// The split draft, the split rule on `Transaction` and the row splice: no network, so they run in parallel.
@MainActor
struct SplitDraftTests {
    private func money(_ text: String) -> Money {
        Money(Decimal(string: text)!)
    }

    private func transaction(
        _ amount: String,
        id: UUID = UUID(),
        bucket: Bucket = .personal,
        pending: Bool = false,
        parentId: UUID? = nil,
        isSplit: Bool = false
    ) -> Transaction {
        Transaction(
            id: id,
            accountId: UUID(),
            postedDate: CalendarDate(year: 2026, month: 10, day: 8),
            transactedAt: nil,
            amount: money(amount),
            descriptionRaw: "CORNER HARDWARE",
            merchant: nil,
            categoryId: nil,
            bucket: bucket,
            source: .manual,
            externalId: nil,
            pending: pending,
            reviewed: false,
            notes: nil,
            eventId: nil,
            parentId: parentId,
            reimbursementId: nil,
            isSplit: isSplit,
            createdAt: .now,
            updatedAt: .now
        )
    }

    private func draft(_ amount: String = "-100.00", bucket: Bucket = .personal) -> SplitDraft {
        SplitDraft(parent: transaction(amount, bucket: bucket))
    }

    private func reason(_ draft: SplitDraft) -> SplitDraft.Invalid? {
        guard case .failure(let rule) = draft.validated() else { return nil }
        return rule
    }

    private func shown(_ draft: SplitDraft) -> [String] {
        draft.entries.map { draft.amountText(of: $0) }
    }

    // MARK: Defaults

    @Test func personalParentStartsAsBusinessAndPersonal() {
        #expect(draft(bucket: .personal).entries.map(\.bucket) == [.business, .personal])
    }

    @Test func businessParentStartsAsBusinessAndPersonal() {
        #expect(draft(bucket: .business).entries.map(\.bucket) == [.business, .personal])
    }

    @Test(arguments: [Bucket.reimbursable, .transfer, .income, .other("gift")])
    func otherParentStartsWithBothPartsInItsBucket(_ bucket: Bucket) {
        #expect(draft(bucket: bucket).entries.map(\.bucket) == [bucket, bucket])
    }

    @Test func addedPartOfAReimbursableParentIsReimbursable() {
        var draft = draft(bucket: .reimbursable)
        draft.setAmountText("40", for: draft.entries[1].id)
        draft.addPart()
        #expect(draft.entries.last?.bucket == .reimbursable)
    }

    @Test func newDraftGivesTheWholeAmountToTheLastPart() {
        let draft = draft()
        #expect(shown(draft) == ["", "100.00"])
        #expect(draft.remaining == money("0"))
        #expect(reason(draft) == .zeroPart)
    }

    // MARK: Remainder

    @Test func lastPartTakesTheRemainder() {
        var draft = draft()
        draft.setAmountText("60", for: draft.entries[0].id)
        #expect(shown(draft) == ["60", "40.00"])
        #expect(draft.isBalanced)
        #expect(reason(draft) == nil)
    }

    @Test func typingInTheLastPartStopsItTakingTheRemainder() {
        var draft = draft()
        draft.setAmountText("60", for: draft.entries[0].id)
        draft.setAmountText("30", for: draft.entries[1].id)
        #expect(shown(draft) == ["60", "30"])
        #expect(draft.remaining == money("10"))
    }

    // MARK: Signs

    @Test func negativeParentSendsPositiveEntriesNegated() throws {
        var draft = draft()
        draft.setAmountText("60", for: draft.entries[0].id)
        draft.setCategory(UUID(), for: draft.entries[1].id)
        let parts = try draft.validated().get()
        #expect(parts.map(\.amount) == [money("-60"), money("-40")])
        #expect(parts.map(\.bucket) == [.business, .personal])
        #expect(parts[0].categoryId == nil)
        #expect(parts[1].categoryId != nil)
    }

    @Test func positiveParentKeepsPositiveAmounts() throws {
        var draft = draft("100.00")
        draft.setAmountText("25.25", for: draft.entries[0].id)
        #expect(try draft.validated().get().map(\.amount) == [money("25.25"), money("74.75")])
    }

    // MARK: Validity

    @Test func zeroPartIsInvalid() {
        var draft = draft()
        draft.setAmountText("0", for: draft.entries[0].id)
        #expect(!draft.isValid(draft.entries[0]))
        #expect(reason(draft) == .zeroPart)
    }

    @Test func unreadablePartIsInvalid() {
        var draft = draft()
        draft.setAmountText("abc", for: draft.entries[0].id)
        #expect(!draft.isValid(draft.entries[0]))
        #expect(reason(draft) == .zeroPart)
    }

    @Test func fullFirstPartLeavesTheLastInvalid() {
        var draft = draft()
        draft.setAmountText("100", for: draft.entries[0].id)
        #expect(!draft.isValid(draft.entries[1]))
        #expect(reason(draft) == .zeroPart)
    }

    @Test func partsOverTheOriginalReportTheExcess() {
        var draft = draft()
        draft.setAmountText("120", for: draft.entries[0].id)
        #expect(reason(draft) == .overAssigned(money("20")))
        #expect(draft.validated().invalidMessage?.contains("exceed the original by $20.00") == true)
    }

    @Test func partsUnderTheOriginalReportWhatIsLeft() {
        var draft = draft()
        draft.setAmountText("60", for: draft.entries[0].id)
        draft.setAmountText("30", for: draft.entries[1].id)
        #expect(reason(draft) == .unassigned(money("10")))
        #expect(draft.validated().invalidMessage?.contains("$10.00 left to assign") == true)
    }

    @Test func everyRuleHasItsOwnMessage() {
        let rules: [SplitDraft.Invalid] = [
            .tooFewParts, .tooManyParts, .zeroPart, .overAssigned(money("1")), .unassigned(money("1")),
        ]
        #expect(Set(rules.map(\.message)).count == rules.count)
        #expect(SplitDraft.Invalid.tooFewParts.message.contains("at least 2"))
        #expect(SplitDraft.Invalid.tooManyParts.message.contains("at most 20"))
    }

    // MARK: Adding and removing

    @Test func partCountStopsAtTwenty() {
        var draft = draft()
        for _ in 0..<30 {
            if let last = draft.entries.last { draft.setAmountText("1", for: last.id) }
            draft.addPart()
        }
        #expect(draft.entries.count == SplitDraft.maxParts)
        #expect(!draft.canAddPart)
    }

    @Test func removingNeverGoesBelowTwoParts() {
        var draft = draft()
        draft.setAmountText("10", for: draft.entries[0].id)
        draft.addPart()
        draft.removeParts(at: IndexSet(0..<3))
        #expect(draft.entries.count == SplitDraft.minParts)
        #expect(!draft.canRemovePart)
    }

    @Test func addingAPartKeepsTheEarlierRemainder() {
        var draft = draft()
        draft.setAmountText("50", for: draft.entries[0].id)
        draft.addPart()
        #expect(shown(draft) == ["50", "50.00", ""])
    }

    @Test func addedPartTakesWhatTheOthersLeave() {
        var draft = draft()
        draft.setAmountText("50", for: draft.entries[0].id)
        draft.addPart()
        draft.setAmountText("20", for: draft.entries[1].id)
        #expect(shown(draft) == ["50", "20", "30.00"])
        #expect(reason(draft) == nil)
    }

    @Test func addPartIsDisabledWhenNothingIsLeftToAssign() {
        var draft = draft()
        draft.setAmountText("100", for: draft.entries[0].id)
        #expect(!draft.canAddPart)
        draft.addPart()
        #expect(draft.entries.count == 2)
        #expect(draft.entries[1].amountText == "")
    }

    @Test func removingTheLastPartKeepsTheNewLastPartsTypedAmount() {
        var draft = draft()
        draft.setAmountText("60", for: draft.entries[0].id)
        draft.setAmountText("20", for: draft.entries[1].id)
        draft.addPart()
        draft.removeParts(at: [2])
        #expect(shown(draft) == ["60", "20"])
    }

    @Test func removingTheLastPartLetsAnUntypedNewLastTakeTheRemainder() {
        var draft = draft()
        draft.setAmountText("60", for: draft.entries[0].id)
        draft.addPart()
        draft.removeParts(at: [2])
        draft.setAmountText("70", for: draft.entries[0].id)
        #expect(shown(draft) == ["70", "30.00"])
    }

    @Test func removingAMiddlePartKeepsTheOthers() {
        var draft = draft()
        draft.setAmountText("50", for: draft.entries[0].id)
        draft.addPart()
        draft.setAmountText("20", for: draft.entries[1].id)
        let kept = [draft.entries[0].id, draft.entries[2].id]
        draft.removeParts(at: [1])
        #expect(draft.entries.map(\.id) == kept)
        #expect(shown(draft) == ["50", "50.00"])
    }

    // MARK: Which transactions split

    @Test func postedTopLevelTransactionCanSplit() {
        #expect(transaction("-10.00").canSplit)
        #expect(transaction("-10.00", isSplit: true).canSplit)
    }

    @Test func childCannotSplit() {
        #expect(!transaction("-10.00", parentId: UUID()).canSplit)
    }

    @Test func pendingTransactionCannotSplit() {
        #expect(!transaction("-10.00", pending: true).canSplit)
    }

    // MARK: Splice

    /// Starts an edit on row `id` whose send waits for `release`, and returns once it was sent.
    private func heldEdit(
        on rows: EditableRows<Transaction>,
        id: UUID,
        leaves: Bool,
        reply: @escaping () throws -> Transaction
    ) async -> (task: Task<Bool, Never>, release: AsyncStream<Void>.Continuation) {
        let (sent, signalSent) = AsyncStream.makeStream(of: Void.self)
        let (released, release) = AsyncStream.makeStream(of: Void.self)
        let task = Task {
            let change = RowEdit<Transaction>(rowName: "Row", patch: TransactionPatch(reviewed: true), leaves: { _ in leaves })
            return await rows.edit(id: id, change) {
                signalSent.yield()
                for await _ in released { break }
                return try reply()
            }
        }
        for await _ in sent { break }
        return (task, release)
    }

    @Test(arguments: [false, true])
    func failedEditOnASplicedOutChildLeavesItRemoved(leaves: Bool) async {
        let parent = transaction("-100.00")
        let child = transaction("-60.00", parentId: parent.id)
        let rows = EditableRows<Transaction>()
        rows.replace(with: [parent, child])
        let edit = await heldEdit(on: rows, id: child.id, leaves: leaves) { throw URLError(.badServerResponse) }

        let fresh = transaction("-100.00", parentId: parent.id)
        rows.splice(parent: parent, children: [fresh], removing: { $0.parentId == parent.id })
        edit.release.yield()
        #expect(await edit.task.value == false)
        #expect(rows.items.map(\.id) == [parent.id, fresh.id])
    }

    @Test func landingEditOnTheParentKeepsItSplit() async {
        let parent = transaction("-100.00")
        let rows = EditableRows<Transaction>()
        rows.replace(with: [parent])
        let edit = await heldEdit(on: rows, id: parent.id, leaves: false) { parent }

        let split = transaction("-100.00", id: parent.id, isSplit: true)
        rows.splice(parent: split, children: [], removing: { $0.parentId == parent.id })
        edit.release.yield()
        #expect(await edit.task.value)
        #expect(rows.items.map(\.isSplit) == [true])
    }

    @Test func spliceOfAnUnlistedParentChangesNothing() {
        let listed = transaction("-5.00")
        let rows = EditableRows<Transaction>()
        rows.replace(with: [listed])
        #expect(!rows.splice(parent: transaction("-100.00"), children: [], removing: { _ in true }))
        #expect(rows.items == [listed])
    }
}

/// Lives in `APIClientTests`' serialized suite because the network tests share `StubURLProtocol`'s one handler.
extension APIClientTests {
    private var parentId: UUID { UUID(uuidString: "AAAAAAAA-0000-4000-8000-000000000001")! }

    private func splitResult() throws -> SplitResult {
        try JSONDecoder.api.decode(SplitResult.self, from: jsonFixture("split-result"))
    }

    private var twoParts: [SplitPart] {
        [
            SplitPart(amount: money("-60"), categoryId: nil, bucket: .business),
            SplitPart(amount: money("-40"), categoryId: nil, bucket: .personal),
        ]
    }

    // MARK: Models

    @Test func splitPartEncodesSignedAmountAndOmitsAbsentKeys() throws {
        let bare = try snakeCaseKeys(SplitPart(amount: money("-60"), categoryId: nil, bucket: nil))
        #expect(Set(bare.keys) == ["amount"])
        #expect(bare["amount"] as? String == "-60.00")

        let category = UUID()
        let full = try snakeCaseKeys(SplitPart(amount: money("-40.5"), categoryId: category, bucket: .personal))
        #expect(Set(full.keys) == ["amount", "category_id", "bucket"])
        #expect(full["amount"] as? String == "-40.50")
        #expect(full["bucket"] as? String == "personal")
        #expect(full["category_id"] as? String == category.uuidString)
    }

    @Test func splitResultFixtureDecodes() throws {
        let result = try splitResult()
        #expect(result.parent.id == parentId)
        #expect(result.parent.isSplit)
        #expect(result.children.count == 2)
        #expect(result.children.allSatisfy { $0.parentId == parentId })
        #expect(result.children.map(\.amount) == [money("-60.00"), money("-40.00")])
        #expect(result.children.map(\.bucket) == [.business, .personal])
    }

    // MARK: Endpoint

    @Test func splitEndpointPostsThePartsToTheParent() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        var requests = reply(200, body: try jsonFixture("split-result")).makeAsyncIterator()

        _ = try await api.splitTransaction(id: parentId, parts: twoParts)
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "POST")
        #expect(request.url?.path() == "/api/transactions/\(uuidString(parentId))/split")
        let body = try #require(bodyData(request))
        let json = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
        let parts = try #require(json["parts"] as? [[String: Any]])
        #expect(Set(json.keys) == ["parts"])
        #expect(parts.count == 2)
        #expect(Set(parts[0].keys) == ["amount", "bucket"])
        #expect(parts[0]["amount"] as? String == "-60.00")
    }

    @Test func splitEndpointDecodesTheParentAndChildren() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        _ = reply(200, body: try jsonFixture("split-result"))

        let result = try await api.splitTransaction(id: parentId, parts: twoParts)
        #expect(result.parent.isSplit)
        #expect(result.children.count == 2)
    }

    // MARK: Model

    private func fixtureObject() throws -> [String: Any] {
        try #require(JSONSerialization.jsonObject(with: jsonFixture("split-result")) as? [String: Any])
    }

    private func pageJSON(_ rows: [[String: Any]]) throws -> Data {
        let items = try rows.map { String(decoding: try JSONSerialization.data(withJSONObject: $0), as: UTF8.self) }
        return Data(#"{"items":[\#(items.joined(separator: ","))],"next_cursor":null}"#.utf8)
    }

    /// A model for `bucket`'s filter, loaded with the fixture's parent (not yet split) and one unrelated row.
    private func splitListModel(bucket: Bucket? = nil) async throws -> (TransactionsModel, APIClient, Transaction) {
        let api = makeClient(fixture.make())
        let model = TransactionsModel()
        model.filter.month = MonthRef(year: 2026, month: 10)
        model.filter.bucket = bucket
        let object = try fixtureObject()
        var unsplit = try #require(object["parent"] as? [String: Any])
        unsplit["is_split"] = false
        var other = try #require((object["children"] as? [[String: Any]])?.first)
        other["parent_id"] = NSNull()
        other["id"] = "dddddddd-0000-4000-8000-000000000001"
        _ = reply(200, body: try pageJSON([unsplit, other]))
        await model.load(api: api)
        return (model, api, try #require(model.transaction(id: parentId)))
    }

    private func envelope(_ message: String) -> Data {
        Data(#"{"error":{"code":"validation_error","message":"\#(message)","request_id":"r1"}}"#.utf8)
    }

    @Test func successfulSplitShowsTheSplitParentWithItsChildrenAfterIt() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api, parent) = try await splitListModel()
        #expect(!parent.isSplit)
        _ = reply(200, body: try jsonFixture("split-result"))

        let saved = await model.split(parent, parts: [], api: api)
        #expect(saved)
        #expect(model.splitError == nil)
        #expect(model.items.count == 4)
        #expect(model.items[0].id == parentId)
        #expect(model.items[0].isSplit)
        #expect(model.items[1...2].allSatisfy { $0.parentId == parentId })
        #expect(model.children(of: parentId).count == 2)
    }

    @Test func resplittingReplacesTheListedChildren() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api, parent) = try await splitListModel()
        _ = reply(200, body: try jsonFixture("split-result"))
        _ = await model.split(parent, parts: [], api: api)

        var object = try fixtureObject()
        var children = try #require(object["children"] as? [[String: Any]])
        children.removeLast()
        children[0]["id"] = "bbbbbbbb-0000-4000-8000-000000000009"
        object["children"] = children
        _ = reply(200, body: try JSONSerialization.data(withJSONObject: object))
        _ = await model.split(model.items[0], parts: [], api: api)

        #expect(model.children(of: parentId).map { uuidString($0.id) } == ["bbbbbbbb-0000-4000-8000-000000000009"])
        #expect(model.items.count == 3)
    }

    @Test func splitUnderABucketFilterListsTheAdmittedChildButDetailShowsBoth() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api, parent) = try await splitListModel(bucket: .business)
        _ = reply(200, body: try jsonFixture("split-result"))
        _ = await model.split(parent, parts: [], api: api)

        #expect(model.items.filter { $0.parentId == parentId }.map(\.bucket) == [.business])
        #expect(model.children(of: parentId).map(\.bucket) == [.business, .personal])
    }

    @Test func splitWhoseChildrenTheFilterHidesStillShowsThemInTheDetail() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api, parent) = try await splitListModel(bucket: .income)
        _ = reply(200, body: try jsonFixture("split-result"))
        _ = await model.split(parent, parts: [], api: api)

        #expect(!model.items.contains { $0.parentId == parentId })
        #expect(model.children(of: parentId).count == 2)
    }

    @Test func childrenOfAParentNotSplitThisSessionAreTheListedOnes() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = TransactionsModel()
        let object = try fixtureObject()
        let parent = try #require(object["parent"] as? [String: Any])
        let children = try #require(object["children"] as? [[String: Any]])
        _ = reply(200, body: try pageJSON([parent] + children))
        await model.load(api: api)

        #expect(model.children(of: parentId).map(\.amount) == [money("-60.00"), money("-40.00")])
    }

    @Test func detailChildrenShowAListedChildsLaterEdit() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api, parent) = try await splitListModel()
        _ = reply(200, body: try jsonFixture("split-result"))
        _ = await model.split(parent, parts: [], api: api)

        var child = try #require((try fixtureObject()["children"] as? [[String: Any]])?.first)
        child["reviewed"] = true
        _ = reply(200, body: try JSONSerialization.data(withJSONObject: child))
        await model.setReviewed(model.items[1], true, api: api)
        #expect(model.children(of: parentId).map(\.reviewed) == [true, false])
    }

    @Test func failedSplitSurfacesTheMessageAndLeavesTheList() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api, parent) = try await splitListModel()
        let rowsBefore = model.items.map(\.id)
        _ = reply(422, body: envelope("Parts must add up to the transaction amount."))

        let saved = await model.split(parent, parts: twoParts, api: api)
        #expect(!saved)
        #expect(model.splitError?.errorDescription == "Parts must add up to the transaction amount.")
        #expect(model.items.map(\.id) == rowsBefore)
        #expect(model.transaction(id: parent.id)?.isSplit == false)
    }

    @Test func resendingTheKeptDraftAfterAFailureSplits() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api, parent) = try await splitListModel()
        var draft = SplitDraft(parent: parent)
        draft.setAmountText("60", for: draft.entries[0].id)
        let parts = try draft.validated().get()
        var requests = reply(422, body: envelope("Try again.")).makeAsyncIterator()
        _ = await model.split(parent, parts: parts, api: api)
        let failed = try #require(await requests.next().flatMap(bodyData))

        requests = reply(200, body: try jsonFixture("split-result")).makeAsyncIterator()
        let saved = await model.split(parent, parts: try draft.validated().get(), api: api)
        let resent = try #require(await requests.next().flatMap(bodyData))
        let sentParts = { (body: Data) in
            (try? JSONSerialization.jsonObject(with: body) as? [String: [[String: String]]])?["parts"]
        }
        #expect(saved)
        #expect(model.splitError == nil)
        #expect(sentParts(failed)?.map { $0["amount"] } == ["-60.00", "-40.00"])
        #expect(sentParts(resent) == sentParts(failed))
    }

    @Test func cancelledSplitSetsNoErrorAndLeavesTheList() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api, parent) = try await splitListModel()
        let rowsBefore = model.items
        let (received, receivedSignal) = AsyncStream.makeStream(of: Void.self)
        StubURLProtocol.respond { request in
            receivedSignal.yield()
            try await Task.sleep(for: .seconds(30))
            return (StubURLProtocol.response(to: request, status: 200), Data())
        }
        let splitting = Task { await model.split(parent, parts: twoParts, api: api) }
        for await _ in received { break }
        splitting.cancel()

        #expect(await splitting.value == false)
        #expect(model.splitError == nil)
        #expect(model.items == rowsBefore)
    }
}
