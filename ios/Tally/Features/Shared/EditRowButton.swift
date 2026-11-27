import SwiftUI

/// A list row that opens an editor: plain text colors, the whole row tappable and a trailing chevron, so it
/// reads like a navigation row instead of an accent-tinted button.
struct EditRowButton<Label: View>: View {
    let action: () -> Void
    @ViewBuilder let label: () -> Label

    var body: some View {
        Button(action: action) {
            HStack {
                label()
                Image(systemName: "chevron.right")
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(.tertiary)
                    .accessibilityHidden(true)
            }
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
    }
}
