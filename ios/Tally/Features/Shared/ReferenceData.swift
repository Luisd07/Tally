import Observation
import SwiftUI

/// Accounts and categories for a screen's pickers and row names, loaded for the screen's lifetime; nothing is
/// stored. A failed load keeps what loaded before and sets `error`; a cancelled one changes nothing.
@Observable
final class ReferenceData {
    private(set) var accounts: [Account] = []
    private(set) var categories: [Category] = []
    /// The latest load failure. Cleared by the next successful load.
    private(set) var error: APIError?

    func load(api: APIClient) async {
        do {
            async let loadedAccounts = api.accounts()
            async let loadedCategories = api.categories()
            let (loaded, named) = try await (loadedAccounts, loadedCategories)
            accounts = loaded
            categories = named
            error = nil
        } catch is CancellationError {
            return
        } catch {
            self.error = APIError(wrapping: error)
        }
    }

    /// Adds a category made on this screen, so its pickers offer it without a reload.
    func add(_ category: Category) {
        categories.append(category)
    }

    /// The name of account `id`, or nil when it is nil or not loaded.
    func accountName(for id: UUID?) -> String? {
        accounts.first { $0.id == id }?.name
    }
}

/// A list section for `reference`'s failed load, with a Retry button; empty when it loaded.
struct ReferenceDataRetrySection: View {
    let reference: ReferenceData

    @Environment(APIClient.self) private var api

    var body: some View {
        if let error = reference.error {
            RetrySection(title: "Couldn't load accounts and categories", error: error) {
                Task { await reference.load(api: api) }
            }
        }
    }
}
