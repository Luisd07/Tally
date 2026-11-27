import SwiftUI

/// One transaction, read live from `model` so optimistic edits and rollbacks show at once. The edit
/// error alert lives on `TransactionsView`, which owns the same model.
struct TransactionDetailView: View {
    let id: UUID
    let model: TransactionsModel
    let categories: [Category]
    let accounts: [Account]

    @Environment(APIClient.self) private var api
    @State private var uploads = ReceiptUploads()
    @State private var recategorizing: Transaction?
    @State private var splitting = false
    @State private var attachTarget: Transaction?
    @State private var merchant = ""
    @State private var notes = ""
    @State private var hasSeededDrafts = false

    var body: some View {
        Group {
            if let transaction = model.transaction(id: id) {
                details(transaction)
            } else {
                ContentUnavailableView(
                    "Not in this list",
                    systemImage: "line.3.horizontal.decrease.circle",
                    description: Text("This transaction isn't in the current list.")
                )
            }
        }
        .navigationTitle(model.transaction(id: id)?.displayName ?? "Transaction")
        .navigationBarTitleDisplayMode(.inline)
        .attachesReceipts(to: $attachTarget, uploads: uploads)
        .offersRuleAfterRecategorize(
            $recategorizing, transaction: { $0 }, categories: categories, otherSheetPresented: splitting
        ) {
            await model.recategorize($0, categoryId: $1, bucket: $2, api: api)
        }
    }

    private func details(_ transaction: Transaction) -> some View {
        Form {
            factsSection(transaction)
            classificationSection(transaction)
            merchantSection(transaction)
            notesSection(transaction)
            childrenSection(transaction)
            ReceiptsSection(
                transaction: transaction, categories: categories, model: model, reloadToken: uploads.completedCount
            )
            Section {
                Button("Attach receipt", systemImage: "paperclip") { attachTarget = transaction }
                if transaction.canSplit {
                    Button("Split", systemImage: "square.split.2x1") { splitting = true }
                }
            }
        }
        .onAppear { seedDrafts(from: transaction) }
        .sheet(isPresented: $splitting) {
            SplitSheet(transaction: transaction, categories: categories, model: model)
        }
    }

    @ViewBuilder
    private func childrenSection(_ transaction: Transaction) -> some View {
        let children = model.children(of: transaction.id)
        if transaction.isSplit, !children.isEmpty {
            Section("Split into") {
                ForEach(children) { child in
                    LabeledContent {
                        Text(child.amount.formatted())
                    } label: {
                        Text(child.bucket.label)
                        Text(categories.label(for: child) ?? "No category")
                    }
                }
            }
        }
    }

    private func factsSection(_ transaction: Transaction) -> some View {
        Section {
            row("Amount", transaction.amount.formatted())
            row("Posted", transaction.postedDate.formatted(.dateTime.year().month().day()))
            if let transactedAt = transaction.transactedAt {
                row("Transacted", transactedAt.formatted(date: .abbreviated, time: .shortened))
            }
            row("Account", accounts.first { $0.id == transaction.accountId }?.name ?? "Unknown account")
            row("Source", transaction.source.label)
            row("Pending", transaction.pending ? "Yes" : "No")
            row("Split", transaction.isSplit ? "Yes" : "No")
            row("Description", transaction.descriptionRaw)
        }
    }

    private func classificationSection(_ transaction: Transaction) -> some View {
        Section {
            row("Category", categories.label(for: transaction) ?? "…")
            row("Bucket", transaction.bucket.label)
            Button("Edit category") { recategorizing = transaction }
            Toggle("Reviewed", isOn: reviewedBinding(transaction))
        }
    }

    private func merchantSection(_ transaction: Transaction) -> some View {
        Section("Merchant") {
            TextField("Merchant", text: $merchant)
                .submitLabel(.done)
                .onSubmit { Task { await model.setMerchant(transaction, merchant, api: api) } }
            Button("Save merchant") { Task { await model.setMerchant(transaction, merchant, api: api) } }
                .disabled(merchant.trimmedOrNil == transaction.merchant)
        }
    }

    private func notesSection(_ transaction: Transaction) -> some View {
        Section("Notes") {
            TextField("Notes", text: $notes, axis: .vertical)
            Button("Save notes") { Task { await model.setNotes(transaction, notes, api: api) } }
                .disabled(notes.trimmedOrNil == transaction.notes)
        }
    }

    private func seedDrafts(from transaction: Transaction) {
        guard !hasSeededDrafts else { return }
        hasSeededDrafts = true
        merchant = transaction.merchant ?? ""
        notes = transaction.notes ?? ""
    }

    private func row(_ title: String, _ value: String) -> some View {
        LabeledContent(title, value: value)
    }

    private func reviewedBinding(_ transaction: Transaction) -> Binding<Bool> {
        Binding(
            get: { transaction.reviewed },
            set: { reviewed in Task { await model.setReviewed(transaction, reviewed, api: api) } }
        )
    }
}
