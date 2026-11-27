import SwiftUI

/// The period a screen shows, as a left-aligned "‹ October 2026 ›" row in the body, with Previous and Next as
/// small accent chevrons. VoiceOver reads the period as one adjustable value; the chevrons stay separate buttons.
struct PeriodSwitcher: View {
    let title: String
    /// What the period is called, e.g. "Month", for VoiceOver and the buttons' labels.
    let unit: String
    let canGoBack: Bool
    let canGoForward: Bool
    let back: () -> Void
    let forward: () -> Void

    var body: some View {
        HStack(spacing: 4) {
            chevron("Previous \(unit.lowercased())", systemImage: "chevron.left", enabled: canGoBack, action: back)
            Text(title)
                .font(.title3.weight(.semibold))
                .monospacedDigit()
                .accessibilityLabel(unit)
                .accessibilityValue(title)
                .accessibilityAdjustableAction { direction in
                    switch direction {
                    case .increment: if canGoForward { forward() }
                    case .decrement: if canGoBack { back() }
                    @unknown default: break
                    }
                }
            chevron("Next \(unit.lowercased())", systemImage: "chevron.right", enabled: canGoForward, action: forward)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func chevron(_ label: String, systemImage: String, enabled: Bool, action: @escaping () -> Void) -> some View {
        Button(label, systemImage: systemImage, action: action)
            .labelStyle(.iconOnly)
            .font(.body.weight(.semibold))
            .frame(minWidth: 32, minHeight: 32)
            .contentShape(.rect)
            .buttonStyle(.borderless)
            .disabled(!enabled)
    }
}

extension PeriodSwitcher {
    /// Switches `month`; Next stops at `latest`.
    init(month: Binding<MonthRef>, latest: MonthRef) {
        self.init(
            title: month.wrappedValue.title(),
            unit: "Month",
            canGoBack: true,
            canGoForward: month.wrappedValue < latest,
            back: { month.wrappedValue = month.wrappedValue.previous },
            forward: { month.wrappedValue = month.wrappedValue.next }
        )
    }
}
