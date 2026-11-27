import SwiftUI

/// A whole screen with nothing to show: what goes here and, when there is one, the next step as a button.
struct EmptyState: View {
    let title: String
    let systemImage: String
    let description: String
    var actionTitle: String?
    var action: (() -> Void)?

    var body: some View {
        ContentUnavailableView {
            Label(title, systemImage: systemImage)
        } description: {
            Text(description)
        } actions: {
            if let actionTitle, let action {
                Button(actionTitle, action: action).buttonStyle(.borderedProminent)
            }
        }
    }
}
