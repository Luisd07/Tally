import Foundation
import Observation

/// The Categories screen's data: every category, archived ones included, grouped by kind. Nothing is stored: rows
/// live for the screen's lifetime. Loading follows `LatestLoad`'s rules; an edit waits for the server, then lands
/// its reply in the list.
@Observable
final class CategoriesModel {
    /// Every category, by name.
    private(set) var categories: [Category] = []
    /// The latest failed `update`, shown by the screen that sent it. Cleared when the next one starts.
    private(set) var updateError: APIError?

    private let loader = LatestLoad()

    var error: APIError? { loader.error }
    var hasData: Bool { loader.hasData }

    /// Active expense categories; a kind the app doesn't know is listed here too.
    var expense: [Category] { categories.filter { !$0.archived && $0.kind != .income } }
    var income: [Category] { categories.filter { !$0.archived && $0.kind == .income } }
    var archived: [Category] { categories.filter(\.archived) }

    /// Loads every category. Only the latest `load` lands; a cancelled one changes nothing, and a failed one sets
    /// `error` and keeps the rows.
    func load(api: APIClient) async {
        guard let loaded = await loader.load(fetch: { try await api.categories() }) else { return }
        categories = loaded.sortedByName
    }

    /// Lists a category made elsewhere (the New category sheet).
    func add(_ category: Category) {
        replace(category)
    }

    /// Sends `patch` for `category` and lists the server's version. Returns whether it saved; on failure
    /// `updateError` says why (nothing for a cancelled one) and the list is unchanged.
    @discardableResult
    func update(_ category: Category, patch: CategoryPatch, api: APIClient) async -> Bool {
        updateError = nil
        return await loader.land(
            { try await api.updateCategory(id: category.id, patch: patch) },
            apply: { replace($0) },
            reload: { await load(api: api) },
            failed: { updateError = $0 }
        )
    }

    /// Archives or restores `category`.
    @discardableResult
    func setArchived(_ category: Category, to archived: Bool, api: APIClient) async -> Bool {
        await update(category, patch: CategoryPatch(archived: archived), api: api)
    }

    private func replace(_ category: Category) {
        var rows = categories.filter { $0.id != category.id }
        rows.append(category)
        categories = rows.sortedByName
    }
}

private extension [Category] {
    var sortedByName: [Category] {
        sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
    }
}
