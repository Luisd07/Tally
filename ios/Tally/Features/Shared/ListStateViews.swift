import SwiftUI

/// A list section for a failed request: the error in red and a Retry button.
struct RetrySection: View {
    let title: String
    let error: APIError
    let retry: () -> Void

    var body: some View {
        Section {
            Label(error.localizedDescription, systemImage: "exclamationmark.triangle")
                .foregroundStyle(.red)
            Button("Retry", action: retry)
        } header: {
            Text(title)
        }
    }
}

/// Shown instead of a list when its first load failed.
struct LoadFailedView: View {
    let title: String
    let error: APIError
    let retry: () -> Void

    var body: some View {
        ContentUnavailableView {
            Label(title, systemImage: "exclamationmark.triangle")
        } description: {
            Text(error.localizedDescription)
        } actions: {
            Button("Retry", action: retry)
        }
    }
}

/// The spinner row at the end of a list while the next page loads.
struct LoadingMoreRow: View {
    var body: some View {
        ProgressView().frame(maxWidth: .infinity).listRowSeparator(.hidden)
    }
}

extension View {
    /// Alerts titled `title` with `message` until it is acknowledged; `clear` is called on dismissal.
    func failureAlert(_ title: LocalizedStringKey, message: String?, clear: @escaping () -> Void) -> some View {
        alert(
            title,
            isPresented: Binding(get: { message != nil }, set: { if !$0 { clear() } }),
            presenting: message
        ) { _ in
            Button("OK", action: clear)
        } message: { message in
            Text(message)
        }
    }

    /// Alerts with `failure`'s message until it is acknowledged; `clear` is called on dismissal.
    func editFailureAlert(_ failure: EditFailure?, clear: @escaping () -> Void) -> some View {
        failureAlert("Couldn't save", message: failure?.message, clear: clear)
    }

    /// Calls `action` when this row appears as the last of a list that has more pages.
    func loadsMore(whenLast isLast: Bool, hasMore: Bool, action: @escaping () -> Void) -> some View {
        onAppear {
            if isLast, hasMore { action() }
        }
    }
}
