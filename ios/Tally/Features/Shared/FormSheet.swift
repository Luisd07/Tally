import Observation
import SwiftUI

/// An add sheet's save state. `draftId` stays the same for the sheet's lifetime, so a retry after a failed Save
/// sends the same id and can't create a second row.
@Observable
final class DraftSubmission {
    let draftId = UUID()
    private(set) var isSaving = false
    /// The latest failed save, shown in the sheet. Cleared when the next one starts.
    private(set) var failure: APIError?

    /// Runs `create` with `draftId`, unless a save is already running. When it returns false, `failure` is set
    /// from `error` (nil for a cancelled save). Returns whether it saved.
    func submit(_ create: (_ draftId: UUID) async -> Bool, error: () -> APIError?) async -> Bool {
        guard !isSaving else { return false }
        isSaving = true
        failure = nil
        defer { isSaving = false }
        if await create(draftId) { return true }
        failure = error()
        return false
    }
}

/// What a form sheet's Save does.
enum FormSheetSave {
    /// Runs `apply` and closes the sheet at once: an optimistic edit, whose failure shows as the screen's alert.
    case closesAtOnce(_ apply: () -> Void)
    /// Stays open until `submit` answers, with the sheet's draft id so a retry can't create a second row. Closes
    /// when it saved; otherwise shows `failure()` inline (nil for a cancelled save).
    case waitsForServer(_ submit: (_ draftId: UUID) async -> Bool, failure: () -> APIError?)
}

/// Where a form sheet's button that closes it without saving sits.
enum FormSheetDismiss {
    /// Cancel, in the toolbar.
    case cancelInToolbar
    /// A large button of this title, under Save.
    case belowSave(_ title: String)
}

/// An add or edit sheet: `fields` in a form, then the latest failure, the large Save button and, while Save is
/// disabled, why. The button that closes it without saving sits where `dismissButton` says. The sheet can't be
/// swiped away while a save is running, so a failure always has somewhere to show.
struct FormSheet<Fields: View>: View {
    let title: String
    let canSave: Bool
    /// Why Save is disabled, if the fields are invalid.
    let invalidReason: String?
    let save: FormSheetSave
    var saveTitle = "Save"
    var dismissButton = FormSheetDismiss.cancelInToolbar
    @ViewBuilder let fields: Fields

    @Environment(\.dismiss) private var dismiss
    /// Lives as long as the sheet, so its draft id stays the same across retries.
    @State private var submission = DraftSubmission()

    var body: some View {
        NavigationStack {
            Form {
                fields
                saveSection
            }
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if case .cancelInToolbar = dismissButton {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Cancel") { dismiss() }
                            .disabled(submission.isSaving)
                    }
                }
            }
        }
        .interactiveDismissDisabled(submission.isSaving)
    }

    private func runSave() {
        switch save {
        case .closesAtOnce(let apply):
            apply()
            dismiss()
        case .waitsForServer(let submit, let failure):
            Task {
                if await submission.submit(submit, error: failure) { dismiss() }
            }
        }
    }

    private var saveSection: some View {
        Section {
            if let failure = submission.failure {
                Label(failure.localizedDescription, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.red)
            }
            Button(action: runSave) {
                Text(submission.isSaving ? "Saving…" : saveTitle).frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .disabled(!canSave || submission.isSaving)
            if case .belowSave(let dismissTitle) = dismissButton {
                Button { dismiss() } label: {
                    Text(dismissTitle).frame(maxWidth: .infinity)
                }
                .controlSize(.large)
                .disabled(submission.isSaving)
            }
        } footer: {
            if let invalidReason {
                Text(invalidReason)
            }
        }
        .listRowBackground(Color.clear)
    }
}

extension Binding where Value == CalendarDate {
    /// This day as a `Date` for a `DatePicker`: its local midnight, and the picked day back.
    var asDate: Binding<Date> {
        Binding<Date>(
            get: { wrappedValue.date() ?? .now },
            set: { wrappedValue = CalendarDate(date: $0) }
        )
    }
}
