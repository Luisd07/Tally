import SwiftUI
import UniformTypeIdentifiers

/// Imports a bank's CSV, OFX or QFX export into an account: pick the account and the file, map a CSV's columns once
/// per account, then upload. The file is read into memory and never stored. Every control is in the body, not the
/// toolbar.
struct ImportView: View {
    @Environment(APIClient.self) private var api
    @State private var reference = ReferenceData()
    @State private var model = ImportModel()
    @State private var accountId: UUID?
    @State private var isPickingFile = false
    /// Whether the server has AI assist on, so the column guess is offered (#172).
    @State private var aiEnabled = false

    /// CSV, and OFX and QFX by their extensions (a dynamic type when no app declares one).
    private static let contentTypes: [UTType] =
        [.commaSeparatedText] + ImportFile.Kind.ofxExtensions.compactMap { UTType(filenameExtension: $0) }

    var body: some View {
        Form {
            ReferenceDataRetrySection(reference: reference)
            sourceSection
            if model.accountId != nil, model.file?.kind == .csv {
                mappingSection
            }
            importSection
            if let result = model.result {
                resultSection(result)
            }
        }
        .navigationTitle("Import")
        .fileImporter(isPresented: $isPickingFile, allowedContentTypes: Self.contentTypes) { model.pickFile($0) }
        .task { await reference.load(api: api) }
        .task { aiEnabled = (try? await api.aiUsage())?.enabled ?? false }
        .task(id: accountId) { await model.select(accountId: accountId, api: api) }
    }

    private var sourceSection: some View {
        Section {
            Picker("Account", selection: $accountId) {
                Text("Choose…").tag(UUID?.none)
                ForEach(reference.accounts.filter(\.active)) { Text($0.name).tag(UUID?.some($0.id)) }
            }
            Button("Choose file") { isPickingFile = true }
            if let file = model.file {
                LabeledContent("File", value: file.name)
            }
        } footer: {
            Text("A CSV, OFX or QFX export from your bank. Rows on or after the account's first synced day, and rows "
                + "already imported, are skipped.")
        }
    }

    @ViewBuilder
    private var mappingSection: some View {
        switch model.mappingLoad {
        case .idle, .loading:
            Section("Columns") { ProgressView() }
        case .failed(let error):
            RetrySection(title: "Couldn't load the column mapping", error: error) {
                Task { await model.reloadMapping(api: api) }
            }
        case .loaded(let saved):
            if model.isEditingMapping {
                if aiEnabled { guessSection }
                ImportMappingEditor(model: model, canCancel: saved != nil)
            } else if let saved {
                savedMappingSection(saved)
            }
        }
    }

    private var guessSection: some View {
        Section {
            Button {
                Task { await model.guessMapping(api: api) }
            } label: {
                Label(model.isGuessingMapping ? "Guessing…" : "Guess the columns", systemImage: "sparkles")
            }
            .disabled(model.isGuessingMapping)
            if let guessError = model.guessError {
                Text(guessError).font(.footnote).foregroundStyle(.red)
            }
        } footer: {
            Text("Sends the header and the first 5 rows with every number except dates hidden, so no amounts, "
                + "balances or account numbers leave the server. Check the guess before saving.")
        }
    }

    private func savedMappingSection(_ mapping: ImportMapping) -> some View {
        Section("Columns") {
            LabeledContent("Date", value: "\(mapping.dateColumn) · \(DateFormatPreset.label(for: mapping.dateFormat))")
            LabeledContent("Description", value: mapping.descriptionColumn)
            switch mapping.amount {
            case .single(let column):
                LabeledContent("Amount", value: "\(column) · \(mapping.signConvention.label)")
            case .debitCredit(let debit, let credit):
                LabeledContent("Debit", value: debit)
                LabeledContent("Credit", value: credit)
            }
            ForEach(model.missingColumns, id: \.self) { column in
                Label("This file has no column “\(column)”", systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.red)
            }
            Button("Edit mapping") { model.editMapping() }
        }
    }

    private var importSection: some View {
        Section {
            if let failure = model.importFailure {
                Label(failure, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.red)
            }
            Button {
                Task { await model.importFile(api: api) }
            } label: {
                Text(model.isImporting ? "Importing…" : "Import").frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .disabled(!model.canImport)
        }
        .listRowBackground(Color.clear)
    }

    private func resultSection(_ result: ImportResult) -> some View {
        Section("Result") {
            if let name = model.resultFileName {
                LabeledContent("File", value: name)
            }
            Text(result.summary)
            ForEach(Array(result.errors.enumerated()), id: \.offset) { _, error in
                Text("Row \(error.row): \(error.message)")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

/// Edits the selected account's CSV column mapping, with column pickers fed from the picked file's first row and
/// a preview of how the draft reads its first data row.
private struct ImportMappingEditor: View {
    @Bindable var model: ImportModel
    /// Whether there is a saved mapping to go back to.
    let canCancel: Bool

    @Environment(APIClient.self) private var api

    var body: some View {
        columnsSection
        if let preview = model.preview {
            ImportPreviewSection(preview: preview)
        }
        actionsSection(invalidMessage: model.draft.validated.invalidMessage)
    }

    private var columnsSection: some View {
        Section {
            Toggle("First row is a header", isOn: Binding(
                get: { model.draft.hasHeader },
                set: { model.draft.setHasHeader($0) }
            ))
            columnPicker("Date", selection: $model.draft.dateColumn)
            columnPicker("Description", selection: $model.draft.descriptionColumn)
            amountPickers
            dateFormatPickers
        } header: {
            Text("Columns")
        } footer: {
            Text("Saved for this account, so its next file maps the same way.")
        }
    }

    @ViewBuilder
    private var amountPickers: some View {
        Picker("Amounts", selection: $model.draft.amountMode) {
            Text("One column").tag(ImportMappingDraft.AmountMode.single)
            Text("Debit and credit").tag(ImportMappingDraft.AmountMode.debitCredit)
        }
        switch model.draft.amountMode {
        case .single:
            columnPicker("Amount", selection: $model.draft.amountColumn)
            Picker("Sign", selection: $model.draft.signConvention) {
                ForEach(SignConvention.pickerOptions(including: model.draft.signConvention), id: \.self) {
                    Text($0.label).tag($0)
                }
            }
        case .debitCredit:
            columnPicker("Debit", selection: $model.draft.debitColumn)
            columnPicker("Credit", selection: $model.draft.creditColumn)
        }
    }

    @ViewBuilder
    private var dateFormatPickers: some View {
        Picker("Date format", selection: $model.draft.datePreset) {
            ForEach(DateFormatPreset.allCases, id: \.self) { Text($0.label).tag($0) }
        }
        if model.draft.datePreset == .custom {
            TextField("Format, like %m/%d/%Y", text: $model.draft.customDateFormat)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
        }
    }

    private func actionsSection(invalidMessage: String?) -> some View {
        Section {
            if let error = model.mappingSaveError {
                Label(error.localizedDescription, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.red)
            }
            Button {
                Task { await model.saveMapping(api: api) }
            } label: {
                Text(model.isSavingMapping ? "Saving…" : "Save mapping").frame(maxWidth: .infinity)
            }
            .controlSize(.large)
            .disabled(invalidMessage != nil || model.isSavingMapping)
            if canCancel {
                Button { model.cancelEditingMapping() } label: {
                    Text("Cancel").frame(maxWidth: .infinity)
                }
                .controlSize(.large)
                .disabled(model.isSavingMapping)
            }
        } footer: {
            if let invalidMessage {
                Text(invalidMessage)
            }
        }
        .listRowBackground(Color.clear)
    }

    private func columnPicker(_ title: String, selection: Binding<String>) -> some View {
        Picker(title, selection: selection) {
            Text("Choose…").tag("")
            ForEach(model.columnOptions(including: selection.wrappedValue), id: \.self) {
                Text(columnLabel($0)).tag($0)
            }
        }
    }

    /// A header name as is; an index as "Column 1" for "0".
    private func columnLabel(_ column: String) -> String {
        guard !model.draft.hasHeader, let index = Int(column) else { return column }
        return "Column \(index + 1)"
    }
}

/// The file's first data row as the draft reads it, or why each field can't be read.
private struct ImportPreviewSection: View {
    let preview: ImportPreview

    var body: some View {
        Section {
            LabeledContent("Date") {
                field(preview.date.map { $0.formatted(.dateTime.month(.abbreviated).day().year()) })
            }
            LabeledContent("Amount") { field(preview.amount.map(ImportPreview.amountText)) }
            LabeledContent("Description") { field(preview.description) }
        } header: {
            Text("First row")
        } footer: {
            Text("How this mapping reads the file. Check the day and month and whether spending shows as money out.")
        }
    }

    @ViewBuilder
    private func field(_ value: Result<String, ImportPreview.Problem>) -> some View {
        switch value {
        case .success(let text): Text(text)
        case .failure(let problem): Text(problem.message).foregroundStyle(.secondary)
        }
    }
}
