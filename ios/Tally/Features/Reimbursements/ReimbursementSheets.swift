import SwiftUI

/// Starts a draft report from the selected expenses. Stays open until the server answers; a failure shows inline.
struct NewReportSheet: View {
    let model: ReimbursementsModel
    let transactionIds: [UUID]
    let onCreated: () -> Void

    @Environment(APIClient.self) private var api
    @State private var noteText = ""

    private var form: Result<ReimbursementForm, ReimbursementForm.Invalid> {
        ReimbursementForm.validated(noteText: noteText)
    }

    var body: some View {
        FormSheet(
            title: "New report",
            canSave: form.invalidMessage == nil,
            invalidReason: form.invalidMessage,
            save: .waitsForServer(create, failure: { model.createError })
        ) {
            TextField("Note (optional)", text: $noteText)
            Text("\(transactionIds.count) selected")
        }
    }

    private func create(id: UUID) async -> Bool {
        guard case .success(let form) = form else { return false }
        let created = await model.createReport(id: id, note: form.note, transactionIds: transactionIds, api: api)
        if created { onCreated() }
        return created
    }
}

/// Submits a draft report as of a day. Stays open until the server answers; a failure shows inline.
struct SubmitReportSheet: View {
    let model: ReimbursementsModel
    let report: Reimbursement

    @Environment(APIClient.self) private var api
    @State private var date = CalendarDate(date: .now)

    var body: some View {
        FormSheet(
            title: "Submit report",
            canSave: true,
            invalidReason: nil,
            save: .waitsForServer(
                { _ in await model.submit(report, on: date, api: api) },
                failure: { model.stepError?.error }
            )
        ) {
            DatePicker("Submitted", selection: $date.asDate, displayedComponents: .date)
        }
    }
}

/// Marks a submitted report paid: the day, no earlier than the submitted one, and the reimbursement income entry
/// that settled it. Stays open until the server answers; a failure shows inline.
struct MarkPaidSheet: View {
    let model: ReimbursementsModel
    let report: Reimbursement

    @Environment(APIClient.self) private var api
    @State private var date: CalendarDate
    @State private var entries = OneShotLoad<[IncomeEntry]>()
    @State private var entryId: UUID?

    init(model: ReimbursementsModel, report: Reimbursement) {
        self.model = model
        self.report = report
        let today = CalendarDate(date: .now)
        _date = State(initialValue: report.submittedDate.map { max($0, today) } ?? today)
    }

    var body: some View {
        FormSheet(
            title: "Mark paid",
            canSave: entryId != nil,
            invalidReason: entryId == nil ? "Pick the income entry that paid this report." : nil,
            save: .waitsForServer({ _ in await markPaid() }, failure: { model.stepError?.error })
        ) {
            DatePicker("Paid", selection: $date.asDate, in: earliestPaidDate..., displayedComponents: .date)
            entryPicker
        }
        .task { await loadEntries() }
    }

    /// The server refuses a paid date before the submitted one.
    private var earliestPaidDate: Date {
        report.submittedDate?.date() ?? .distantPast
    }

    @ViewBuilder
    private var entryPicker: some View {
        if let error = entries.error {
            Label(error.localizedDescription, systemImage: "exclamationmark.triangle").foregroundStyle(.red)
        } else if let available = entries.value, !available.isEmpty {
            Picker("Income entry", selection: $entryId) {
                Text("Choose…").tag(UUID?.none)
                ForEach(available) { entry in
                    Text("\(entry.date.formatted()) · \(entry.displayName) \(entry.received?.formatted() ?? "")")
                        .tag(UUID?.some(entry.id))
                }
            }
            .pickerStyle(.inline)
        } else if entries.value != nil {
            Text("No unlinked reimbursement income yet.").foregroundStyle(.secondary)
            NavigationLink("Add one in Income") {
                // Popping back reloads, so an entry added there shows up in the picker.
                IncomeView().onDisappear { Task { await loadEntries() } }
            }
        } else {
            ProgressView().frame(maxWidth: .infinity)
        }
    }

    private func loadEntries() async {
        await entries.load { try await model.incomeAvailableToSettle(api: api) }
        if !(entries.value ?? []).contains(where: { $0.id == entryId }) { entryId = nil }
    }

    private func markPaid() async -> Bool {
        guard let entryId else { return false }
        return await model.markPaid(report, on: date, incomeEntryId: entryId, api: api)
    }
}
