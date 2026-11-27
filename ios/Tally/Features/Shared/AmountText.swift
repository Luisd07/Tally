import SwiftUI

/// The only way an amount is drawn: monospaced digits, with the sign and color its `Role` gives it. The font
/// comes from the surrounding view.
struct AmountText: View {
    enum Role: Equatable {
        /// A signed flow as the API sends it: money out is negative and shows unsigned in the primary color;
        /// money in shows "+" in `MoneyIn` green.
        case flow
        /// A magnitude with no direction (a total, a cost, a budget): unsigned, primary.
        case amount
        /// A transfer or a figure for context only: unsigned, secondary.
        case neutral
    }

    enum Tone: Equatable {
        case primary, moneyIn, secondary
    }

    /// What `AmountText` shows for an amount, apart from the view.
    struct Presentation: Equatable {
        let text: String
        let tone: Tone

        init(_ money: Money, role: Role) {
            let magnitude = money.amount < 0 ? money.negated : money
            switch role {
            case .flow where money.amount > 0:
                text = "+" + magnitude.formatted()
                tone = .moneyIn
            case .flow, .amount:
                text = magnitude.formatted()
                tone = .primary
            case .neutral:
                text = magnitude.formatted()
                tone = .secondary
            }
        }
    }

    let money: Money
    let role: Role

    init(_ money: Money, role: Role = .amount) {
        self.money = money
        self.role = role
    }

    var body: some View {
        let presentation = Presentation(money, role: role)
        Text(presentation.text)
            .monospacedDigit()
            .foregroundStyle(presentation.tone.style)
    }
}

private extension AmountText.Tone {
    /// Colors rather than hierarchical styles, so an amount inside secondary text isn't faded a level further.
    var style: Color {
        switch self {
        case .primary: .primary
        case .moneyIn: Color(.moneyIn)
        case .secondary: .secondary
        }
    }
}

extension Transaction {
    /// How its amount is drawn: a transfer is neutral, everything else is a flow.
    var amountRole: AmountText.Role {
        bucket == .transfer ? .neutral : .flow
    }
}
