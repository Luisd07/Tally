import Foundation
import Observation

/// The Reimbursements screen's data: what is owed back, the reimbursable expenses in no report, and the expense
/// reports. Nothing is stored: it lives for the screen's lifetime. Loading and edits follow `LoadedList`'s rules,
/// and the three come from one load, so the header never disagrees with the lists. The list has no filter, so
/// after any accepted write the lot is loaded again and what is owed and the unreported expenses match the server.
@Observable
final class ReimbursementsModel: @MainActor ListScreenModel {
    /// The latest failed create; the caller still holds the draft.
    private(set) var createError: APIError?
    /// The latest failed submit or mark paid, already rolled back. The sheet that sent it shows it, not the
    /// screen's alert. Cleared when the next one starts.
    private(set) var stepError: EditFailure?
    /// What is still owed back to you, as the server counts it (the number Home shows).
    private(set) var owed = Money(0)
    /// Settled reimbursable expenses in no report, newest first: what a report can still take.
    private(set) var unsubmitted: [Transaction] = []

    /// Reports that aren't paid yet: drafts first, then the rest, each in the server's order (newest first).
    var openReports: [Reimbursement] {
        let open = list.items.filter { $0.status != .paid }
        return open.filter(\.isDraft) + open.filter { !$0.isDraft }
    }

    /// Paid reports, the most recently paid first.
    var paidReports: [Reimbursement] {
        list.items.filter { $0.status == .paid }.sorted { lhs, rhs in
            switch (lhs.paidDate, rhs.paidDate) {
            case let (left?, right?) where left != right: return left > right
            case (_?, nil): return true
            case (nil, _?): return false
            default: return lhs.createdAt > rhs.createdAt
            }
        }
    }

    /// The reports expenses can be added to: the drafts.
    var reportsAvailableToAdd: [Reimbursement] { list.items.filter(\.isDraft) }

    let list = LoadedList<Reimbursement>()
    /// The reimbursement income entries by id, for the paid reports' "Paid by" lines.
    private let settlingIncome = OneShotLoad<[UUID: IncomeEntry]>()

    private struct Snapshot {
        let reports: [Reimbursement]
        let owed: Money
        let unsubmitted: [Transaction]
    }

    /// Report `id` as listed, or nil when it isn't.
    func report(id: UUID) -> Reimbursement? {
        list.items.first { $0.id == id }
    }

    /// The income entry that settled `report`, once loaded. Nil when none did or the lookup failed.
    func settledBy(_ report: Reimbursement) -> IncomeEntry? {
        report.incomeEntryId.flatMap { settlingIncome.value?[$0] }
    }

    /// Loads the reports, what is owed and the unreported expenses together. Only the latest `load` lands; a
    /// cancelled one changes nothing, and a failed one (any of the three) sets `error` and keeps the data.
    /// Then looks up the income entries that settled the paid reports.
    func load(api: APIClient) async {
        guard let snapshot = await list.load({ _ in try await Self.snapshot(api: api) }, rows: \.reports)
        else { return }
        owed = snapshot.owed
        unsubmitted = snapshot.unsubmitted
        await loadSettlingIncome(api: api)
    }

    /// Starts a draft report holding `transactionIds` under `id`, which the caller keeps across retries
    /// (a `FormSheet`'s draft id) so sending it again can't create a second report. Returns false on
    /// failure, with `createError` set unless it was cancelled.
    func createReport(id: UUID, note: String?, transactionIds: [UUID], api: APIClient) async -> Bool {
        createError = nil
        let draft = ReimbursementDraft(
            id: id,
            note: note?.trimmedOrNil,
            transactionIds: transactionIds.isEmpty ? nil : transactionIds
        )
        return await list.land(
            { try await api.createReimbursement(draft) },
            apply: { list.rows.upsert($0) },
            reload: { await load(api: api) },
            failed: { createError = $0 }
        )
    }

    /// Adds `transactionIds` to a draft `report`. Returns false, sending nothing, when it isn't a draft.
    @discardableResult
    func add(_ transactionIds: [UUID], to report: Reimbursement, api: APIClient) async -> Bool {
        guard !transactionIds.isEmpty, listedIsDraft(report) else { return false }
        return await update(report, patch: ReimbursementPatch(addTransactionIds: transactionIds), api: api)
    }

    /// Takes `transactionIds` out of a draft `report`. Returns false, sending nothing, when it isn't a draft.
    @discardableResult
    func remove(_ transactionIds: [UUID], from report: Reimbursement, api: APIClient) async -> Bool {
        guard !transactionIds.isEmpty, listedIsDraft(report) else { return false }
        return await update(report, patch: ReimbursementPatch(removeTransactionIds: transactionIds), api: api)
    }

    /// Submits a draft `report` as of `date`: it shows as submitted at once, and goes back to a draft with
    /// `stepError` set if the server refuses. Returns false, sending nothing, when it isn't a draft.
    @discardableResult
    func submit(_ report: Reimbursement, on date: CalendarDate, api: APIClient) async -> Bool {
        let patch = ReimbursementPatch(status: .submitted, submittedDate: .some(date))
        return await step(report, to: .submitted, patch: patch, api: api)
    }

    /// Marks a submitted `report` paid on `date`, settled by the reimbursement income entry `incomeEntryId`.
    /// It shows as paid at once and rolls back with `stepError` set if the server refuses (an entry already
    /// linked to another report, say). Returns false, sending nothing, when it isn't submitted.
    @discardableResult
    func markPaid(_ report: Reimbursement, on date: CalendarDate, incomeEntryId: UUID, api: APIClient) async -> Bool {
        let patch = ReimbursementPatch(status: .paid, paidDate: .some(date), incomeEntryId: .some(incomeEntryId))
        return await step(report, to: .paid, patch: patch, api: api)
    }

    /// The expenses in `report`, newest first.
    func expenses(of report: Reimbursement, api: APIClient) async throws -> [Transaction] {
        try await api.allTransactions(TransactionQuery(reimbursementId: report.id))
    }

    /// The reimbursement income entries, of any month, that no report is settled by yet: what "Mark paid" offers.
    func incomeAvailableToSettle(api: APIClient) async throws -> [IncomeEntry] {
        let taken = Set(list.items.compactMap(\.incomeEntryId))
        return try await api.income(type: .reimbursement).items.filter { !taken.contains($0.id) }
    }

    /// The three parts of a load, fetched together; the expenses are every page, filtered to the ones the server
    /// counts as unreported (no report, not pending).
    private static func snapshot(api: APIClient) async throws -> Snapshot {
        async let page = api.reimbursements()
        async let owed = api.owedToYou()
        async let expenses = api.allTransactions(TransactionQuery(bucket: .reimbursable, excludeSplitParents: true))
        return try await Snapshot(
            reports: page.items,
            owed: owed.owed,
            unsubmitted: expenses.filter { $0.reimbursementId == nil && !$0.pending }
        )
    }

    /// Looks up the income entries behind the paid reports, when any is linked to one.
    private func loadSettlingIncome(api: APIClient) async {
        guard paidReports.contains(where: { $0.incomeEntryId != nil }) else { return }
        await settlingIncome.load {
            let page = try await api.income(type: .reimbursement)
            return Dictionary(page.items.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        }
    }

    /// Whether the listed version of `report` is a draft (the screen's copy may be older than the rows).
    private func listedIsDraft(_ report: Reimbursement) -> Bool {
        self.report(id: report.id)?.isDraft ?? false
    }

    /// Whether the listed version of `report` is one step from `target`; the server takes no other move.
    private func canStep(_ report: Reimbursement, to target: ReimbursementStatus) -> Bool {
        self.report(id: report.id)?.status.next == target
    }

    /// Moves `report` on to `target` with `patch`, reporting a refusal through `stepError`.
    private func step(
        _ report: Reimbursement, to target: ReimbursementStatus, patch: ReimbursementPatch, api: APIClient
    ) async -> Bool {
        stepError = nil
        guard canStep(report, to: target) else { return false }
        return await update(report, patch: patch, failed: { self.stepError = $0 }, api: api)
    }

    /// Applies `patch` at once, confirms it with the server, then loads everything again. A failure goes to
    /// `failed` when it is given, else to `editError`.
    private func update(
        _ report: Reimbursement,
        patch: ReimbursementPatch,
        failed: ((EditFailure) -> Void)? = nil,
        api: APIClient
    ) async -> Bool {
        await list.update(
            id: report.id,
            rowName: report.displayName,
            patch: patch,
            failed: failed,
            send: { try await api.updateReimbursement(id: report.id, patch: patch) },
            reload: { await load(api: api) }
        )
    }
}
