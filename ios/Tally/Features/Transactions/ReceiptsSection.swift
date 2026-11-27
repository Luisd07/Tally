import SwiftUI

/// The transaction's receipts and what Claude read off each (#170). A reading is only shown: "Use these details"
/// sets the merchant and category, and anything that doesn't add up is flagged rather than trusted.
struct ReceiptsSection: View {
    let transaction: Transaction
    let categories: [Category]
    let model: TransactionsModel
    /// Changes when an upload finishes, so the new receipt shows.
    let reloadToken: Int

    @Environment(APIClient.self) private var api
    @State private var receipts: [ReceiptWithReading] = []

    var body: some View {
        Group {
            if !receipts.isEmpty {
                Section("Receipts") {
                    ForEach(receipts) { receipt in
                        ReceiptReadingRows(
                            receipt: receipt, transaction: transaction, categories: categories
                        ) { reading in
                            Task { await model.useReceiptReading(transaction, reading, api: api) }
                        }
                    }
                }
            }
        }
        .task(id: reloadToken) { await load() }
    }

    private func load() async {
        // A failed load leaves the section as it was; the receipts are a nice-to-have here.
        if let loaded = try? await api.receipts(transactionId: transaction.id) { receipts = loaded }
    }
}

private struct ReceiptReadingRows: View {
    let receipt: ReceiptWithReading
    let transaction: Transaction
    let categories: [Category]
    let use: (ReceiptReading) -> Void

    var body: some View {
        let uploaded = receipt.uploadedAt.formatted(date: .abbreviated, time: .shortened)
        if let reading = receipt.reading, reading.status == .ready {
            VStack(alignment: .leading, spacing: 6) {
                Label(summary(reading), systemImage: "sparkles")
                if !reading.lineItems.isEmpty {
                    DisclosureGroup("\(reading.lineItems.count) line items") {
                        ForEach(Array(reading.lineItems.enumerated()), id: \.offset) { _, item in
                            LabeledContent(item.description, value: item.amount.formatted())
                        }
                        if let tax = reading.tax { LabeledContent("Tax", value: tax.formatted()) }
                        if let tip = reading.tip { LabeledContent("Tip", value: tip.formatted()) }
                    }
                    .font(.subheadline)
                }
                ForEach(flags(reading), id: \.self) { flag in
                    Label(flag, systemImage: "exclamationmark.triangle")
                        .font(.footnote)
                        .foregroundStyle(.orange)
                }
                if canUse(reading) {
                    Button("Use these details") { use(reading) }
                }
            }
        } else if receipt.reading?.status == .failed {
            LabeledContent("Receipt from \(uploaded)", value: "Couldn't read it")
        } else {
            LabeledContent("Receipt from \(uploaded)", value: "Not read yet")
        }
    }

    /// "Chipotle · $21.60 · Oct 22, 2026 · Dining"
    private func summary(_ reading: ReceiptReading) -> String {
        let date = reading.receiptDate.flatMap { $0.date(in: .current) }?.formatted(date: .abbreviated, time: .omitted)
        let parts = [
            reading.merchant, reading.total?.formatted(), date, categories.name(for: reading.categoryId),
        ]
        let text = parts.compactMap { $0 }.joined(separator: " · ")
        return text.isEmpty ? "Read, but nothing came out" : text
    }

    private func flags(_ reading: ReceiptReading) -> [String] {
        var flags: [String] = []
        if reading.addsUp == false { flags.append("The line items, tax and tip don't add up to the total.") }
        if reading.matchesTransaction == false {
            flags.append("The total isn't this charge of \(transaction.amount.negated.formatted()).")
        }
        return flags
    }

    private func canUse(_ reading: ReceiptReading) -> Bool {
        let newMerchant = reading.merchant.map { $0 != transaction.merchant } ?? false
        let newCategory = reading.categoryId.map { $0 != transaction.categoryId } ?? false
        return newMerchant || newCategory
    }
}
