import SwiftUI

/// Home as the month at a glance, starting with the `PeriodSwitcher`.
struct MonthGlanceSections: View {
    let glance: MonthGlance
    @Binding var month: MonthRef
    let latest: MonthRef

    var body: some View {
        LeftThisMonth(glance: glance, switcher: switcher, month: $month)
    }

    private var switcher: PeriodSwitcher { PeriodSwitcher(month: $month, latest: latest) }
}

/// What's left and the pace up top, then what needs the user, where the money went, income and the bills to come.
private struct LeftThisMonth: View {
    let glance: MonthGlance
    let switcher: PeriodSwitcher
    @Binding var month: MonthRef

    var body: some View {
        Section {
            VStack(alignment: .leading, spacing: 8) {
                switcher
                SummaryHeader(
                    glance.left < Money(0) ? "Over by" : "Left this month",
                    amount: glance.left
                ) {
                    HStack(spacing: 4) {
                        Text("of")
                        AmountText(glance.limit, role: .neutral)
                        Text(glance.hasBudget ? "budget" : "expected income")
                    }
                } accessory: {
                    VStack(alignment: .leading, spacing: 6) {
                        PaceBar(fraction: glance.spentFraction, elapsed: glance.elapsed, isOver: glance.isOverPace)
                        PaceCaption(glance: glance)
                        SyncStatus(lastUpdated: glance.home.lastUpdated)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
        NeedsYouSection(glance: glance)
        categoriesSection
        incomeSection
        UpcomingBillsSection(glance: glance)
    }

    /// Each category's share of the month, under the month's total and its bucket split.
    private var categoriesSection: some View {
        Section {
            ForEach(glance.dashboard.byCategory) { category in
                CategoryBudgetRow(category: category, totalSpent: glance.spent, month: month)
            }
            NavigationLink("See breakdown") { DashboardView(month: month) }
        } header: {
            HStack {
                Text("Spent")
                Spacer()
                AmountText(glance.spent, role: .neutral)
            }
        } footer: {
            HStack(spacing: 4) {
                ForEach(Array(glance.home.spendingByBucket.buckets.enumerated()), id: \.element.bucket) { index, item in
                    if index > 0 { Text("·") }
                    Text(item.bucket.label)
                    AmountText(item.spent, role: .neutral)
                }
            }
        }
    }

    private var incomeSection: some View {
        let remaining = glance.expected - glance.received
        return Section("Income") {
            LabeledContent("Received") { AmountText(glance.received, role: .flow) }
            LabeledContent("Expected") { AmountText(glance.expected) }
            if remaining > Money(0) {
                LabeledContent("Still to come") { AmountText(remaining, role: .neutral) }
            }
        }
    }
}

/// The month's remaining bills by date, with what they add up to.
private struct UpcomingBillsSection: View {
    let glance: MonthGlance

    var body: some View {
        Section {
            if glance.bills.isEmpty {
                Label("No more bills this month", systemImage: "checkmark.circle")
                    .foregroundStyle(.secondary)
            }
            ForEach(glance.bills) { bill in
                HStack(alignment: .firstTextBaseline) {
                    Text(bill.date.formatted())
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                        .frame(width: 56, alignment: .leading)
                    Text(bill.name)
                    Spacer()
                    if let amount = bill.amount {
                        AmountText(amount)
                    } else {
                        Text("—").foregroundStyle(.secondary)
                    }
                }
            }
        } header: {
            HStack {
                Text("Upcoming bills")
                Spacer()
                if !glance.bills.isEmpty { AmountText(glance.billsTotal, role: .neutral) }
            }
        }
    }
}

/// A thin bar of spending against the limit, with a tick where an even pace would be today.
private struct PaceBar: View {
    let fraction: Double
    let elapsed: Double
    let isOver: Bool

    private let trackHeight: CGFloat = 6
    private let tickHeight: CGFloat = 14

    var body: some View {
        GeometryReader { proxy in
            let width = proxy.size.width
            ZStack(alignment: .leading) {
                Capsule().fill(Color.secondary.opacity(0.25))
                    .frame(height: trackHeight)
                Capsule().fill(isOver ? Color(.attention) : Color.accentColor)
                    .frame(width: width * min(max(fraction, 0), 1), height: trackHeight)
                Capsule().fill(.primary)
                    .frame(width: 2, height: tickHeight)
                    .offset(x: min(max(width * elapsed - 1, 0), width - 2))
            }
            .frame(maxHeight: .infinity)
        }
        .frame(height: tickHeight)
        .accessibilityHidden(true)
    }
}

/// "$X under pace", or "$X over pace" with an `Attention` icon.
private struct PaceCaption: View {
    let glance: MonthGlance

    var body: some View {
        HStack(spacing: 4) {
            if glance.isOverPace {
                Image(systemName: "exclamationmark.circle").foregroundStyle(Color(.attention))
            }
            AmountText(glance.underPace, role: .neutral)
            Text(glance.isOverPace ? "over pace" : "under pace")
        }
        .font(.caption)
        .foregroundStyle(.secondary)
    }
}

/// What waits on the user, each to its own screen; only the ones that aren't zero.
private struct NeedsYouSection: View {
    let glance: MonthGlance

    var body: some View {
        Section("Needs you") {
            if glance.toReview == 0, glance.missingReceipts == 0, glance.home.owedToYou == Money(0) {
                Label("Nothing needs you", systemImage: "checkmark.circle")
                    .foregroundStyle(.secondary)
            }
            if glance.toReview > 0 {
                LabeledContent("To review in Inbox") { Text("\(glance.toReview)").monospacedDigit() }
            }
            if glance.missingReceipts > 0 {
                NavigationLink { TaxView() } label: {
                    LabeledContent("Missing receipts") { Text("\(glance.missingReceipts)").monospacedDigit() }
                }
            }
            if glance.home.owedToYou > Money(0) {
                NavigationLink { ReimbursementsView() } label: {
                    LabeledContent("Owed to you") { AmountText(glance.home.owedToYou) }
                }
            }
        }
    }
}
