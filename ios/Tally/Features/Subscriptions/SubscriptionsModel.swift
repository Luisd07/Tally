import Foundation
import Observation

/// The Subscriptions screen's data: subscriptions and bills, each ordered by next charge date. Nothing is
/// stored: rows live for the screen's lifetime. Loading, the inactive filter and edits follow `LoadedList`'s rules.
@Observable
final class SubscriptionsModel: @MainActor ListScreenModel {
    /// The latest failed create; the caller still holds the draft.
    private(set) var createError: APIError?
    /// Whether closed subscriptions are listed too. Change it with `setIncludeInactive`.
    var includeInactive: Bool { list.includeInactive }

    /// Everything that isn't a bill, kinds the app doesn't know included, by next charge date.
    var subscriptions: [Subscription] { list.items.filter { $0.kind != .bill } }
    /// The bills (the car payment), by next charge date.
    var bills: [Subscription] { list.items.filter { $0.kind == .bill } }

    /// Monthly and annual cost of the active rows in `subscriptions`, negative.
    var subscriptionTotals: SubscriptionTotals { totals.excluding(.bill) }
    /// Monthly and annual cost of the active bills, negative.
    var billTotals: SubscriptionTotals { totals[kind: .bill] }

    let list = LoadedList<Subscription>(isActive: \.active)
    /// The server's on load, recomputed from the rows after a local change.
    private var totals = SubscriptionTotalsByKind.empty

    /// Loads the list. Only the latest `load` lands; a cancelled one changes nothing, and a failed one sets
    /// `error` and keeps the rows.
    func load(api: APIClient) async {
        guard let page = await list.load({ try await api.subscriptions(includeInactive: $0) }, rows: \.items)
        else { return }
        totals = page.totals
        sortRows()
    }

    /// Switches between active-only and all subscriptions, then reloads. Turning it off drops the closed rows at once.
    func setIncludeInactive(_ include: Bool, api: APIClient) async {
        await list.setIncludeInactive(include) { await load(api: api) }
    }

    /// Creates `draft` and inserts the server's row in date order (unless it is closed and closed ones are
    /// hidden), recomputing the totals locally. Returns false on failure, with `createError` set unless it was
    /// cancelled; the draft keeps its id, so sending it again can't duplicate the row. A load sent before the
    /// create is dropped so it can't take the new row off the list, then sent again so the current filter's rows land.
    func create(_ draft: SubscriptionDraft, api: APIClient) async -> Bool {
        createError = nil
        return await list.land(
            { try await api.createSubscription(draft) },
            apply: { created in
                guard created.active || includeInactive else { return }
                list.rows.upsert(created)
                rowsChanged()
            },
            reload: { await load(api: api) },
            failed: { createError = $0 }
        )
    }

    /// Applies `patch` to the row at once, then confirms it with the server. A row that turns inactive leaves
    /// the list unless closed ones are shown. On failure the row goes back and `editError` says why.
    /// Returns whether the server accepted the edit.
    @discardableResult
    func update(_ row: Subscription, patch: SubscriptionPatch, api: APIClient) async -> Bool {
        await list.update(
            id: row.id,
            rowName: row.name,
            patch: patch,
            changed: { _ in rowsChanged() },
            send: { try await api.updateSubscription(id: row.id, patch: patch) },
            reload: { await load(api: api) }
        )
    }

    /// Pauses (`false`) or resumes (`true`) `row`.
    func setActive(_ row: Subscription, to active: Bool, api: APIClient) async {
        await update(row, patch: SubscriptionPatch(active: active), api: api)
    }

    /// Next charge date ascending, undated last, then name.
    private func sortRows() {
        list.rows.sort { lhs, rhs in
            switch (lhs.nextDue, rhs.nextDue) {
            case let (left?, right?) where left != right: return left < right
            case (_?, nil): return true
            case (nil, _?): return false
            default:
                let order = lhs.name.localizedCaseInsensitiveCompare(rhs.name)
                return order == .orderedSame ? lhs.id.uuidString < rhs.id.uuidString : order == .orderedAscending
            }
        }
    }

    /// Restores the order and recomputes the totals the way the server does.
    private func rowsChanged() {
        sortRows()
        totals = SubscriptionTotalsByKind(costing: list.items)
    }
}
