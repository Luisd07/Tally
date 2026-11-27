import SwiftUI

/// A full-width tappable row that opens a menu of choices; picking one closes it, so no extra tap is needed.
struct MenuRow<Choices: View>: View {
    let title: String
    let value: String
    @ViewBuilder let choices: () -> Choices

    var body: some View {
        Menu {
            choices()
        } label: {
            HStack {
                Text(title).foregroundStyle(.primary)
                Spacer()
                Text(value).foregroundStyle(.secondary)
                Image(systemName: "chevron.up.chevron.down").font(.footnote).foregroundStyle(.secondary)
            }
            .padding(.horizontal)
            .frame(maxWidth: .infinity, minHeight: 48)
            .background(.fill.tertiary, in: RoundedRectangle(cornerRadius: 12))
            .contentShape(Rectangle())
        }
        .accessibilityLabel("\(title), \(value)")
    }
}
