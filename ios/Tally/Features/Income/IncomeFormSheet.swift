import SwiftUI

/// Adds an income entry, or edits `entry`. An add keeps its id across a failed Save, so a retry can't
/// create a second row; an edit sends only the fields that changed.
struct IncomeFormSheet: View {
    let model: IncomeModel
    let entry: IncomeEntry?
    let month: MonthRef

    @Environment(APIClient.self) private var api
    @State private var date: Date
    @State private var type: IncomeType
    @State private var description: String
    @State private var expectedText: String
    @State private var receivedText: String

    init(model: IncomeModel, entry: IncomeEntry?, month: MonthRef) {
        self.model = model
        self.entry = entry
        self.month = month
        let day = entry?.date ?? Self.defaultDate(in: month)
        _date = State(initialValue: day.date() ?? .now)
        _type = State(initialValue: entry?.type ?? .eventPay)
        _description = State(initialValue: entry?.description ?? "")
        _expectedText = State(initialValue: entry?.expected?.inputText ?? "")
        _receivedText = State(initialValue: entry?.received?.inputText ?? "")
    }

    /// Today when it falls in `month`, otherwise the month's first day.
    private static func defaultDate(in month: MonthRef) -> CalendarDate {
        let today = CalendarDate(date: .now)
        return MonthRef(containing: today) == month ? today : CalendarDate(year: month.year, month: month.month, day: 1)
    }

    /// What the fields hold, or nil while an amount is invalid or both are blank.
    private var form: IncomeForm? {
        IncomeForm(
            date: CalendarDate(date: date), type: type, description: description,
            expectedText: expectedText, receivedText: receivedText
        )
    }

    /// A valid form; for an edit, one that changes something.
    private var canSave: Bool {
        guard let form else { return false }
        guard let entry else { return true }
        return IncomePatch(from: entry, to: form) != nil
    }

    var body: some View {
        FormSheet(
            title: entry == nil ? "Add income" : "Edit income",
            canSave: canSave,
            invalidReason: nil,
            save: save
        ) {
            fields
            rulesSection
        }
    }

    @ViewBuilder
    private var fields: some View {
        DatePicker("Date", selection: $date, displayedComponents: .date)
        Picker("Type", selection: $type) {
            ForEach(IncomeType.pickerOptions(including: type), id: \.self) { Text($0.label).tag($0) }
        }
        TextField("Description", text: $description)
        TextField("Expected", text: $expectedText)
            .keyboardType(.decimalPad)
        TextField("Received", text: $receivedText)
            .keyboardType(.decimalPad)
    }

    private var rulesSection: some View {
        Section {
            Text("Enter an expected amount, a received amount, or both. Amounts can't be negative.")
                .font(.footnote).foregroundStyle(.secondary)
        }
    }

    /// An edit closes at once and lets the model apply the change optimistically; a failure shows as the
    /// screen's alert. An add stays open until the server answers; a failure shows inline and keeps the draft's
    /// id for the retry.
    private var save: FormSheetSave {
        guard let entry else { return .waitsForServer(create, failure: { model.createError }) }
        return .closesAtOnce { saveEdit(of: entry) }
    }

    private func saveEdit(of entry: IncomeEntry) {
        guard let form, let patch = IncomePatch(from: entry, to: form) else { return }
        Task { await model.update(entry, patch: patch, api: api) }
    }

    private func create(id: UUID) async -> Bool {
        guard let form else { return false }
        return await model.create(IncomeDraft(id: id, form: form), api: api)
    }
}
