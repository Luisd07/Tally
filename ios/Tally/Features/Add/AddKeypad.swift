import SwiftUI

/// The 3x4 amount keypad: 1-9, ".", 0, backspace.
struct AddKeypad: View {
    let model: AddModel

    private let columns = Array(repeating: GridItem(.flexible(), spacing: 10), count: 3)

    var body: some View {
        LazyVGrid(columns: columns, spacing: 10) {
            ForEach(1...9, id: \.self) { digit in
                key(String(digit)) { model.press(digit) }
            }
            key(".") { model.pressDecimal() }
            key("0") { model.press(0) }
            Button { model.backspace() } label: {
                Image(systemName: "delete.left")
                    .font(.title2)
                    .frame(maxWidth: .infinity, minHeight: 52)
            }
            .buttonStyle(.bordered)
            .accessibilityLabel("Delete")
        }
    }

    private func key(_ title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title)
                .font(.title.weight(.medium))
                .frame(maxWidth: .infinity, minHeight: 52)
        }
        .buttonStyle(.bordered)
        .accessibilityLabel(title == "." ? "Decimal point" : title)
    }
}
