import SwiftUI

/// List sections for `year`'s tax summary: gross receipts, the deductible spend by tax line and the total.
/// The screen loads `summary` for `year`; this section shows it and calls `retry` to load it again.
struct TaxSummarySection: View {
    let year: Int
    let summary: KeyedValue<Int, TaxSummary>
    let retry: () -> Void

    var body: some View {
        if summary.hasData, let value = summary.value {
            loaded(value)
        } else if let error = summary.error {
            RetrySection(title: "Couldn't load the tax summary", error: error) { retry() }
        } else {
            Section { ProgressView().frame(maxWidth: .infinity) }
        }
    }

    @ViewBuilder
    private func loaded(_ value: TaxSummary) -> some View {
        if let error = summary.error {
            RetrySection(title: "Couldn't refresh", error: error) { retry() }
        }
        if value.isEmpty {
            Section {
                ContentUnavailableView("No business income or expenses in \(String(year))", systemImage: "tray")
            }
        } else {
            Section("Tax summary") {
                LabeledContent("Gross receipts", value: value.grossReceipts.formatted())
            }
            lines(value)
            Section {
                LabeledContent("Total deductible", value: value.totalDeductible.formatted()).bold()
            } footer: {
                notes(value.notes)
            }
        }
    }

    /// The tax lines, with the mileage breakdown under line 9. With no line 9 (every mile unpriced, or only
    /// actual car costs), the breakdown follows the lines on its own so the miles still show.
    @ViewBuilder
    private func lines(_ value: TaxSummary) -> some View {
        let hasCarTruck = value.expensesByLine.contains { $0.taxLine == TaxLineSpend.carTruck }
        if !value.expensesByLine.isEmpty || value.mileage.hasActivity {
            Section("Expenses by tax line") {
                ForEach(value.expensesByLine) { line in
                    lineRow(line)
                    if line.taxLine == TaxLineSpend.carTruck { mileageRows(value.mileage) }
                }
                if !hasCarTruck && value.mileage.hasActivity { mileageRows(value.mileage) }
                uncategorizedRow(value.uncategorizedBusiness)
            }
        } else if value.uncategorizedBusiness.count > 0 {
            Section { uncategorizedRow(value.uncategorizedBusiness) }
        }
    }

    /// How line 9 was built: business miles at the rates in force, parking and tolls, and what was left out.
    private func mileageRows(_ mileage: MileageSummary) -> some View {
        Group {
            LabeledContent("Business miles", value: mileage.businessMiles.formatted())
            LabeledContent("Mileage") {
                TrailingDetail(value: mileage.mileageAmount.formatted()) {
                    ForEach(mileage.rates, id: \.effectiveFrom) { used in
                        Text("\(used.miles.formatted()) at \(used.rate.perMileText)").captionStyle()
                    }
                }
            }
            LabeledContent("Parking & tolls", value: mileage.parkingTolls.formatted())
            if mileage.unpricedMiles.value > 0 {
                LabeledContent("Unpriced miles") {
                    TrailingDetail(value: mileage.unpricedMiles.formatted()) {
                        Text("No rate in force").captionStyle()
                    }
                }
            }
            if mileage.excludedActualCarCosts.amount > 0 {
                LabeledContent("Actual car costs (not deductible)", value: mileage.excludedActualCarCosts.formatted())
            }
            if let total = mileage.totalMiles {
                LabeledContent("Odometer total", value: total.formatted())
            }
        }
        .font(.subheadline)
        .padding(.leading)
    }

    private func lineRow(_ line: TaxLineSpend) -> some View {
        LabeledContent(line.displayName) {
            TrailingDetail(value: line.spent.formatted()) {
                Group {
                    if let pct = line.deductiblePct { Text("\(pct)% deductible") }
                    Text(line.deductible.formatted())
                }
                .captionStyle()
            }
        }
    }

    @ViewBuilder
    private func uncategorizedRow(_ uncategorized: UncategorizedBusiness) -> some View {
        if uncategorized.count > 0 {
            LabeledContent("Uncategorized business") {
                TrailingDetail(value: uncategorized.spent.formatted()) {
                    Text(transactionCount(uncategorized.count)).captionStyle()
                }
            }
        }
    }

    private func transactionCount(_ count: Int) -> String {
        count == 1 ? "1 transaction" : "\(count) transactions"
    }

    private func notes(_ notes: [String]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(Array(notes.enumerated()), id: \.offset) { Text($0.element) }
        }
    }
}

private extension View {
    func captionStyle() -> some View {
        font(.caption).foregroundStyle(.secondary)
    }
}
