import SwiftUI

/// The IRS standard mileage rates, oldest first: each is in force from its date until the next one starts.
struct MileageRatesView: View {
    @Environment(APIClient.self) private var api
    @State private var model = MileageRatesModel()
    @State private var sheet: RateSheet?

    var body: some View {
        content
            .navigationTitle("Rates")
            .navigationBarTitleDisplayMode(.inline)
            .task { await load() }
            .refreshable { await load() }
            .reloadOnReturnToForeground { await load() }
            .sheet(item: $sheet) { sheet in
                MileageRateFormSheet(model: model, rate: sheet.rate)
            }
            .editFailureAlert(model.editError, clear: model.clearEditError)
    }

    @ViewBuilder
    private var content: some View {
        if model.hasData {
            list
        } else if let error = model.error {
            LoadFailedView(title: "Couldn't load Rates", error: error) { Task { await load() } }
        } else {
            ProgressView().frame(maxHeight: .infinity)
        }
    }

    private var list: some View {
        List {
            if let error = model.error {
                RetrySection(title: "Couldn't refresh", error: error) { Task { await load() } }
            }
            Section {
                Button("Add rate", systemImage: "plus.circle.fill") { sheet = .add }
            }
            Section {
                if model.rates.isEmpty {
                    Text("No rates").foregroundStyle(.secondary)
                }
                ForEach(model.rates) { rate in
                    Button(rate.label) { sheet = .edit(rate) }
                        .foregroundStyle(.primary)
                        .swipeActions(edge: .trailing) {
                            Button("Delete", role: .destructive) { Task { await model.delete(rate, api: api) } }
                        }
                }
            } footer: {
                Text("Miles on a date before the first rate add nothing to the deduction.")
            }
        }
    }

    private func load() async {
        await model.load(api: api)
    }
}

/// Adds a rate, or edits `rate`, by `TripFormSheet`'s rules. A date another rate already starts on is refused
/// with the server's message.
private struct MileageRateFormSheet: View {
    let model: MileageRatesModel
    let rate: MileageRate?

    @Environment(APIClient.self) private var api
    @State private var fields: MileageRateFields

    init(model: MileageRatesModel, rate: MileageRate?) {
        self.model = model
        self.rate = rate
        _fields = State(initialValue: rate.map(MileageRateFields.init(editing:)) ?? MileageRateFields())
    }

    private var form: Result<MileageRateForm, MileageFormRule> { fields.validated }

    private var canSave: Bool {
        guard case .success(let form) = form else { return false }
        guard let rate else { return true }
        return MileageRatePatch(from: rate, to: form) != nil
    }

    var body: some View {
        FormSheet(
            title: rate == nil ? "Add rate" : "Edit rate",
            canSave: canSave,
            invalidReason: form.invalidMessage,
            save: save
        ) {
            DatePicker("From", selection: $fields.effectiveFrom.asDate, displayedComponents: .date)
            TextField("Dollars per mile, like 0.725", text: $fields.rateText)
                .keyboardType(.decimalPad)
        }
    }

    private var save: FormSheetSave {
        guard let rate else { return .waitsForServer(create, failure: { model.createError }) }
        return .closesAtOnce {
            guard case .success(let form) = form, let patch = MileageRatePatch(from: rate, to: form) else { return }
            Task { await model.update(rate, patch: patch, api: api) }
        }
    }

    private func create(id: UUID) async -> Bool {
        guard case .success(let form) = form else { return false }
        return await model.create(MileageRateDraft(id: id, effectiveFrom: form.effectiveFrom, rate: form.rate), api: api)
    }
}

private enum RateSheet: Identifiable {
    case add
    case edit(MileageRate)

    var id: String {
        switch self {
        case .add: "add"
        case .edit(let rate): rate.id.uuidString
        }
    }

    var rate: MileageRate? {
        if case .edit(let rate) = self { rate } else { nil }
    }
}
