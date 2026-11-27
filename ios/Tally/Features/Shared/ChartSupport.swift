import SwiftUI

extension View {
    /// Makes a chart one VoiceOver element that reads `label`, then `summary`.
    func chartAccessibility(_ label: String, summary: String) -> some View {
        accessibilityElement(children: .ignore)
            .accessibilityLabel(label)
            .accessibilityValue(summary)
    }
}

/// A row's trailing value with caption lines under it, for a `LabeledContent` that shows more than one line.
struct TrailingDetail<Captions: View>: View {
    let value: String
    @ViewBuilder let captions: () -> Captions

    var body: some View {
        VStack(alignment: .trailing, spacing: 2) {
            Text(value)
            captions()
        }
    }
}
