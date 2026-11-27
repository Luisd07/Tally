import Foundation
import Observation

/// Recent sign-in activity, newest first. Session renewals happen every 15 minutes, so they're hidden unless asked for.
@Observable
final class SignInActivityModel {
    private(set) var events: [AuthEvent] = []
    private(set) var hasLoaded = false
    private(set) var isLoadingMore = false
    private(set) var message: String?
    var showsRenewals = false
    private var nextCursor: String?

    var hasMore: Bool { nextCursor != nil }

    /// The rows on screen: everything, or everything but renewals.
    var visible: [AuthEvent] {
        showsRenewals ? events : events.filter { $0.kind != .refresh }
    }

    var failureCount: Int { events.filter(\.kind.isFailure).count }

    func load(api: APIClient) async {
        do {
            let page = try await api.authEvents()
            events = page.items
            nextCursor = page.nextCursor
            message = nil
        } catch is CancellationError {
            return
        } catch {
            message = error.localizedDescription
        }
        hasLoaded = true
    }

    func loadMore(api: APIClient) async {
        guard let cursor = nextCursor, !isLoadingMore else { return }
        isLoadingMore = true
        defer { isLoadingMore = false }
        do {
            let page = try await api.authEvents(cursor: cursor)
            let known = Set(events.map(\.id))
            events += page.items.filter { !known.contains($0.id) }
            nextCursor = page.nextCursor
        } catch is CancellationError {
            return
        } catch {
            message = error.localizedDescription
        }
    }
}
