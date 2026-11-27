import Charts
import SwiftUI

struct CardLogView: View {
    let card: CardSummary

    @Environment(APIClient.self) private var api
    @State private var state = MonthValue<CardLog>()
    @State private var month = MonthRef()

    var body: some View {
        MonthListScreen(
            title: card.account.name,
            failedTitle: "Couldn't load the log",
            month: $month,
            state: state,
            fetch: { try await api.cardLog(accountId: card.id, month: $0) }
        ) { log in
            if let interest = card.interest {
                Section("Interest and fees, last 12 months") { interestRows(interest) }
            }
            Section("Balance") {
                balanceRow("Starting balance", owed: log.startingOwed, date: log.startingBalanceDate)
            }
            Section("Activity") { activityRows(log) }
            Section("Reconciliation") { reconciliationRows(log) }
        }
    }

    @ViewBuilder
    private func interestRows(_ interest: CardInterest) -> some View {
        Chart(interest.months) { item in
            BarMark(
                x: .value("Month", item.month.firstDay() ?? .distantPast, unit: .month),
                y: .value("Interest", item.amount.chartValue)
            )
            .foregroundStyle(item.month == month ? Color.accentColor : Color.secondary.opacity(0.4))
        }
        .chartXAxis { AxisMarks(values: .stride(by: .month)) { _ in AxisValueLabel(format: .dateTime.month(.narrow)) } }
        .frame(height: 120)
        .chartAccessibility(
            "Interest by month",
            summary: interest.months.map { "\($0.month.title()) \($0.amount.formatted())" }.joined(separator: ", ")
        )
        LabeledContent("This year", value: interest.yearToDate.formatted())
        if card.hasAprMismatch {
            Text(CardSummary.aprMismatchNote).font(.footnote).foregroundStyle(.orange)
        }
    }

    @ViewBuilder
    private func activityRows(_ log: CardLog) -> some View {
        LabeledContent("New charges", value: log.charged.formatted())
        LabeledContent("Payments", value: log.payments.formatted())
        LabeledContent("Interest & fees", value: log.feesCharged.formatted())
        if log.other.amount != 0 {
            LabeledContent("Other", value: log.other.formatted())
        }
    }

    @ViewBuilder
    private func reconciliationRows(_ log: CardLog) -> some View {
        balanceRow("Ending balance", owed: log.endingOwed, date: log.endingBalanceDate)
        if let computed = log.computedEndingOwed {
            LabeledContent("Computed ending", value: computed.formatted())
        }
        switch log.reconciliation(in: month) {
        case .reconciled: differenceRow(Money(0), reconciled: true)
        case .off(let owed): differenceRow(owed, reconciled: false)
        case let other: Text(note(for: other)).font(.footnote).foregroundStyle(.secondary)
        }
    }

    private func balanceRow(_ title: String, owed: Money?, date: CalendarDate?) -> some View {
        LabeledContent(title) {
            TrailingDetail(value: owed?.formatted() ?? "—") {
                if let date {
                    Text("as of \(date.formatted())").font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }

    private func differenceRow(_ difference: Money, reconciled: Bool) -> some View {
        LabeledContent("Difference") {
            HStack {
                if reconciled {
                    Label("Reconciled", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                }
                Text(difference.formatted())
            }
        }
    }

    private func note(for reconciliation: CardLog.Reconciliation) -> String {
        switch reconciliation {
        case .staleEnding(let date): "Balance on \(date.formatted()) is older than the month"
        case .missingBalances(missingStart: true, missingEnd: false): "No starting balance for the month"
        case .missingBalances(missingStart: false, missingEnd: true): "No ending balance for the month"
        case .reconciled, .off, .missingBalances: "Not enough balances to reconcile"
        }
    }
}
