import SwiftUI

extension View {
    /// Runs `action` when the app comes back to the foreground after being backgrounded.
    func reloadOnReturnToForeground(_ action: @escaping () async -> Void) -> some View {
        modifier(ReloadOnReturnToForeground(action: action))
    }
}

private struct ReloadOnReturnToForeground: ViewModifier {
    let action: () async -> Void

    @Environment(\.scenePhase) private var scenePhase
    @State private var wasInBackground = false

    func body(content: Content) -> some View {
        content.onChange(of: scenePhase) { _, phase in
            if phase == .background {
                wasInBackground = true
            } else if phase == .active, wasInBackground {
                wasInBackground = false
                Task { await action() }
            }
        }
    }
}
