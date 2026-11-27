import SwiftUI

/// The start and end odometer readings for `year`. Save replaces them and goes back.
struct OdometerView: View {
    let year: Int

    @Environment(APIClient.self) private var api
    @State private var model = OdometerModel()

    var body: some View {
        content
            .navigationTitle("Odometer \(String(year))")
            .navigationBarTitleDisplayMode(.inline)
            .task { await load() }
    }

    @ViewBuilder
    private var content: some View {
        if model.hasData {
            OdometerForm(year: year, model: model)
        } else if let error = model.error {
            LoadFailedView(title: "Couldn't load the odometer", error: error) { Task { await load() } }
        } else {
            ProgressView().frame(maxHeight: .infinity)
        }
    }

    private func load() async {
        await model.load(year: year, api: api)
    }
}

/// The readings' fields, filled from the reading loaded when it first shows.
private struct OdometerForm: View {
    let year: Int
    let model: OdometerModel

    @Environment(APIClient.self) private var api
    @Environment(\.dismiss) private var dismiss
    @State private var fields: OdometerFields

    init(year: Int, model: OdometerModel) {
        self.year = year
        self.model = model
        _fields = State(initialValue: OdometerFields(editing: model.reading))
    }

    private var input: Result<OdometerInput, MileageFormRule> { fields.validated }

    var body: some View {
        Form {
            Section {
                TextField("Start of year", text: $fields.startText)
                    .keyboardType(.decimalPad)
                TextField("End of year (optional)", text: $fields.endText)
                    .keyboardType(.decimalPad)
                if case .success(let input) = input, let total = input.totalMiles {
                    LabeledContent("Miles driven", value: total.formatted())
                }
            } footer: {
                Text("Your car's odometer at the start and end of the year, for the total miles the tax return asks for.")
            }
            Section {
                if let error = model.saveError {
                    Label(error.localizedDescription, systemImage: "exclamationmark.triangle").foregroundStyle(.red)
                }
                Button(action: save) {
                    Text(model.isSaving ? "Saving…" : "Save").frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .disabled(input.invalidMessage != nil || model.isSaving)
            } footer: {
                if let message = input.invalidMessage {
                    Text(message)
                }
            }
            .listRowBackground(Color.clear)
        }
    }

    private func save() {
        guard case .success(let input) = input else { return }
        Task {
            if await model.save(input, year: year, api: api) { dismiss() }
        }
    }
}
