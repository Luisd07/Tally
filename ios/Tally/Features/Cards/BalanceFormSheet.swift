import SwiftUI

/// Records what a manual card owes on a day. Stays open until the server answers; a failure shows inline.
struct BalanceFormSheet: View {
    let model: CardsModel
    let card: CardSummary

    @Environment(APIClient.self) private var api
    @State private var fields = BalanceFields()

    private var entry: Result<BalanceEntry, BalanceEntry.Invalid> { fields.validated() }

    var body: some View {
        FormSheet(
            title: "Update balance",
            canSave: entry.invalidMessage == nil,
            invalidReason: entry.invalidMessage,
            save: .waitsForServer({ _ in await record() }, failure: { model.balanceError })
        ) {
            TextField("Amount owed", text: $fields.amountText)
                .keyboardType(.decimalPad)
            Toggle("Credit balance", isOn: $fields.isCredit)
            DatePicker(
                "Date", selection: $fields.date.asDate, in: dateRange, displayedComponents: .date
            )
        }
    }

    private var dateRange: ClosedRange<Date> {
        let earliest = BalanceFields.earliestDate.date() ?? .distantPast
        return earliest...max(earliest, BalanceFields.latestDate().date() ?? .now)
    }

    private func record() async -> Bool {
        guard case .success(let entry) = entry else { return false }
        return await model.recordBalance(card, entry: entry, api: api)
    }
}
