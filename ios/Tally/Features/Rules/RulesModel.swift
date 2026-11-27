import Foundation
import Observation

/// The Rules screen's data: every rule, active or not, in the order the server runs them. Nothing is stored: rows
/// live for the screen's lifetime. Loading and edits follow `LoadedList`'s rules; no rule leaves the list by being
/// turned off, and a write changes nothing else on screen, so it loads again only when it dropped a load.
@Observable
final class RulesModel: @MainActor ListScreenModel {
    var rules: [Rule] { list.items }

    let list = LoadedList<Rule>(reloadsAfterEveryWrite: false)

    /// Loads the rules. Only the latest `load` lands; a cancelled one changes nothing, and a failed one sets
    /// `error` and keeps the rows.
    func load(api: APIClient) async {
        await list.load({ _ in try await api.rules() }, rows: { $0 })
    }

    /// Turns `rule` on or off at once, then confirms it with the server. On failure it goes back and `editError`
    /// says why.
    func setActive(_ rule: Rule, to active: Bool, api: APIClient) async {
        let patch = RulePatch(active: active)
        await list.update(
            id: rule.id,
            rowName: rule.pattern,
            patch: patch,
            send: { try await api.updateRule(id: rule.id, patch: patch) },
            reload: { await load(api: api) }
        )
    }

    /// Takes `rule` off the list at once, then deletes it on the server. On failure it goes back where it stood and
    /// `editError` says why.
    func delete(_ rule: Rule, api: APIClient) async {
        await list.remove(
            id: rule.id,
            rowName: rule.pattern,
            send: { try await api.deleteRule(id: rule.id) },
            reload: { await load(api: api) }
        )
    }
}
