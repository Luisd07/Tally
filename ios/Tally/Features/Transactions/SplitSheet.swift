import SwiftUI

/// Splits `transaction` into parts that add up to its amount. Save waits for the server; a failure shows in the
/// sheet and the draft stays as it was.
struct SplitSheet: View {
    let transaction: Transaction
    let categories: [Category]
    let model: TransactionsModel

    @Environment(APIClient.self) private var api
    @State private var draft: SplitDraft
    /// Categories made from this sheet, which `categories` doesn't have yet.
    @State private var created: [Category] = []
    /// The part whose New category sheet is open.
    @State private var creatingFor: UUID?

    private var validated: Result<[SplitPart], SplitDraft.Invalid> { draft.validated() }

    init(transaction: Transaction, categories: [Category], model: TransactionsModel) {
        self.transaction = transaction
        self.categories = categories
        self.model = model
        _draft = State(initialValue: SplitDraft(parent: transaction))
    }

    var body: some View {
        FormSheet(
            title: "Split",
            canSave: validated.invalidMessage == nil,
            invalidReason: validated.invalidMessage,
            save: .waitsForServer({ _ in await save() }, failure: { model.splitError })
        ) {
            headerSection
            partsSection
        }
        .sheet(isPresented: Binding(get: { creatingFor != nil }, set: { if !$0 { creatingFor = nil } })) {
            NewCategorySheet(kind: transaction.amount.amount < 0 ? .expense : .income) { category in
                created.append(category)
                if let part = creatingFor { draft.setCategory(category.id, for: part) }
            }
        }
    }

    private var headerSection: some View {
        Section {
            LabeledContent("Original", value: draft.magnitude.formatted())
            LabeledContent("Remaining") {
                Text(draft.remaining.formatted())
                    .foregroundStyle(draft.isBalanced ? Color.secondary : Color.red)
            }
            if transaction.isSplit {
                Text("This transaction is already split. Saving replaces its existing parts.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
        }
    }

    private var partsSection: some View {
        Section("Parts") {
            ForEach(draft.entries) { part in
                partRow(part)
                    .deleteDisabled(!draft.canRemovePart)
            }
            .onDelete { draft.removeParts(at: $0) }
            Button("Add part", systemImage: "plus") { draft.addPart() }
                .disabled(!draft.canAddPart)
        }
    }

    private func partRow(_ part: SplitDraft.Part) -> some View {
        VStack(alignment: .leading) {
            TextField("0.00", text: Binding(
                get: { draft.amountText(of: part) },
                set: { draft.setAmountText($0, for: part.id) }
            ))
            .keyboardType(.decimalPad)
            .foregroundStyle(draft.isValid(part) ? Color.primary : Color.red)
            Picker("Bucket", selection: Binding(get: { part.bucket }, set: { draft.setBucket($0, for: part.id) })) {
                ForEach(Bucket.pickerOptions(including: part.bucket), id: \.self) { Text($0.label).tag($0) }
            }
            Picker("Category", selection: categoryBinding(for: part)) {
                Text("Same as original").tag(UUID?.none)
                ForEach((categories + created).pickable(keeping: part.categoryId)) { Text($0.name).tag(UUID?.some($0.id)) }
            }
            Button("New category…", systemImage: "plus") { creatingFor = part.id }
                .buttonStyle(.borderless)
        }
    }

    private func save() async -> Bool {
        guard case .success(let parts) = validated else { return false }
        return await model.split(transaction, parts: parts, api: api)
    }

    private func categoryBinding(for part: SplitDraft.Part) -> Binding<UUID?> {
        Binding(get: { part.categoryId }, set: { draft.setCategory($0, for: part.id) })
    }
}
