import Foundation
import Observation

/// The Cards screen's data: each card with its latest balance, by name. Nothing is stored: rows live for the
/// screen's lifetime. Loading, the inactive filter and edits follow `LoadedList`'s rules.
@Observable
final class CardsModel: @MainActor ListScreenModel {
    /// The latest failed balance entry; the caller still holds the entry.
    private(set) var balanceError: APIError?
    /// Whether closed cards are listed too. Change it with `setIncludeInactive`.
    var includeInactive: Bool { list.includeInactive }

    /// The cards, by name.
    var cards: [CardSummary] { list.items }

    /// Interest and fees this year across every card, closed ones too. Nil until loaded; a failed fetch keeps the
    /// last value.
    private(set) var interestSummary: InterestSummary?

    /// A card turned into a loan leaves the list at once, like a closed one.
    let list = LoadedList<CardSummary>(isActive: { $0.account.active && $0.account.kind != .loan })

    /// Loads the cards. Only the latest `load` lands; a cancelled one changes nothing, and a failed one sets
    /// `error` and keeps the rows.
    func load(api: APIClient) async {
        await list.load({ try await api.cards(includeInactive: $0) }, rows: { $0 })
        if let summary = try? await api.interestSummary() { interestSummary = summary }
        if let loaded = try? await api.loans() { loans = loaded }
    }

    /// The active loans, by name. Not part of the card list or the Payoff plan. A failed fetch keeps the last value.
    private(set) var loans: [LoanSummary] = []

    /// The latest failed loan edit; the loan is as the server has it.
    private(set) var loanEditError: EditFailure?

    func clearLoanEditError() {
        loanEditError = nil
    }

    /// Sends `patch` for a loan, then reloads. A failure sets `loanEditError`.
    private func updateLoan(_ loan: CardSummary, patch: AccountPatch, api: APIClient) async -> Bool {
        loanEditError = nil
        do {
            _ = try await api.updateAccount(id: loan.id, patch: patch)
        } catch {
            guard let error = error as? APIError else { return false }
            loanEditError = EditFailure(error: error, rowName: loan.account.name)
            return false
        }
        await load(api: api)
        return true
    }

    /// Switches between active-only and all cards, then reloads. Turning it off drops the closed cards at once.
    func setIncludeInactive(_ include: Bool, api: APIClient) async {
        await list.setIncludeInactive(include) { await load(api: api) }
    }

    /// Applies `patch` to the card at once, then confirms it with the server. A card that turns inactive leaves
    /// the list unless closed ones are shown. On failure the card goes back and `editError` says why.
    /// Returns whether the server accepted the edit.
    @discardableResult
    func update(_ card: CardSummary, patch: AccountPatch, api: APIClient) async -> Bool {
        if card.account.kind == .loan { return await updateLoan(card, patch: patch, api: api) }
        let saved = await list.update(
            id: card.id,
            rowName: card.account.name,
            patch: patch,
            send: { try await api.updateAccount(id: card.id, patch: patch) },
            reload: { await load(api: api) }
        )
        // A new kind moves the account to the loans, which the list's own reload rules don't cover.
        if saved, patch.kind != nil { await load(api: api) }
        return saved
    }

    /// Records `entry` as the card's balance for its day. The card shows it only when the day is not before
    /// the balance shown (the newest wins), and an edit in flight on the card keeps it. Returns false on failure,
    /// with `balanceError` set unless it was cancelled; the caller keeps the entry.
    func recordBalance(_ card: CardSummary, entry: BalanceEntry, api: APIClient) async -> Bool {
        balanceError = nil
        return await list.land(
            { try await api.upsertBalance(accountId: card.id, date: entry.date, balance: entry.balance) },
            apply: { snapshot in list.rows.amend(id: card.id) { $0.recording(snapshot) } },
            reload: { await load(api: api) },
            failed: { balanceError = $0 }
        )
    }
}
