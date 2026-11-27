import SwiftUI

/// Adds a subscription or bill, or edits `row`. An add keeps its id across a failed Save, so a retry can't
/// create a second row; an edit sends only the fields that changed.
struct SubscriptionFormSheet: View {
    let model: SubscriptionsModel
    let row: Subscription?
    let reference: ReferenceData

    @Environment(APIClient.self) private var api
    /// Dates and the match text the sheet doesn't show stay as they are on an edit.
    @State private var fields: SubscriptionFields
    @State private var isCreatingCategory = false

    init(model: SubscriptionsModel, row: Subscription?, reference: ReferenceData) {
        self.model = model
        self.row = row
        self.reference = reference
        _fields = State(initialValue: row.map(SubscriptionFields.init(editing:)) ?? SubscriptionFields())
    }

    /// What the fields hold, or the first rule they break.
    private var form: Result<SubscriptionForm, SubscriptionForm.Invalid> { fields.validated }

    /// A valid form; for an edit, one that changes something.
    private var canSave: Bool {
        guard case .success(let form) = form else { return false }
        guard let row else { return true }
        return SubscriptionPatch(from: row, to: form) != nil
    }

    var body: some View {
        FormSheet(
            title: row == nil ? "Add subscription" : "Edit subscription",
            canSave: canSave,
            invalidReason: form.invalidMessage,
            save: save
        ) {
            details
            pickers
        }
        .sheet(isPresented: $isCreatingCategory) {
            NewCategorySheet(kind: .expense) { category in
                reference.add(category)
                fields.categoryId = category.id
            }
        }
    }

    @ViewBuilder
    private var details: some View {
        TextField("Name", text: $fields.name)
        TextField("Amount", text: $fields.costText)
            .keyboardType(.decimalPad)
        Picker("Cadence", selection: $fields.cadence) {
            ForEach(Cadence.pickerOptions(including: fields.cadence), id: \.self) { Text($0.label).tag($0) }
        }
        Picker("Kind", selection: $fields.kind) {
            ForEach(SubscriptionKind.pickerOptions(including: fields.kind), id: \.self) { Text($0.label).tag($0) }
        }
        Toggle("Next charge date", isOn: $fields.hasNextDue)
        if fields.hasNextDue {
            DatePicker("Date", selection: $fields.nextDue.asDate, displayedComponents: .date)
        }
        Toggle("Active", isOn: $fields.active)
        TextField("Business % (optional)", text: $fields.businessPctText)
            .keyboardType(.numberPad)
    }

    @ViewBuilder
    private var pickers: some View {
        Picker("Account", selection: $fields.accountId) {
            Text("None").tag(UUID?.none)
            ForEach(reference.accounts) { Text($0.name).tag(Optional($0.id)) }
        }
        Picker("Category", selection: $fields.categoryId) {
            Text("None").tag(UUID?.none)
            ForEach(reference.categories.pickable(keeping: fields.categoryId)) { Text($0.name).tag(Optional($0.id)) }
        }
        Button("New category…", systemImage: "plus") { isCreatingCategory = true }
        Picker("Bucket", selection: $fields.bucket) {
            ForEach(Bucket.pickerOptions(including: fields.bucket), id: \.self) { Text($0.label).tag($0) }
        }
    }

    /// An edit closes at once and lets the model apply the change optimistically; a failure shows as the
    /// screen's alert. An add stays open until the server answers; a failure shows inline and keeps the draft's
    /// id for the retry.
    private var save: FormSheetSave {
        guard let row else { return .waitsForServer(create, failure: { model.createError }) }
        return .closesAtOnce { saveEdit(of: row) }
    }

    private func saveEdit(of row: Subscription) {
        guard case .success(let form) = form, let patch = SubscriptionPatch(from: row, to: form) else { return }
        Task { await model.update(row, patch: patch, api: api) }
    }

    private func create(id: UUID) async -> Bool {
        guard case .success(let form) = form else { return false }
        return await model.create(SubscriptionDraft(id: id, form: form), api: api)
    }
}
