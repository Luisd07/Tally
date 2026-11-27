import Observation
import SwiftUI

/// Creates a category for a picker and hands it back to the caller. Save waits for the server, so a duplicate
/// name shows in the sheet and the draft stays as it was.
struct NewCategorySheet: View {
    let onCreated: (Category) -> Void

    @Environment(APIClient.self) private var api
    @State private var model = NewCategoryModel()
    @State private var name = ""
    @State private var kind: CategoryKind
    @State private var budgetText = ""

    /// `kind` is what the picker the sheet came from is for; it can still be changed here.
    init(kind: CategoryKind, onCreated: @escaping (Category) -> Void) {
        self.onCreated = onCreated
        _kind = State(initialValue: kind)
    }

    private var trimmedName: String? { name.trimmedOrNil }
    private var budget: Money?? {
        guard budgetText.trimmedOrNil != nil else { return .some(nil) }
        return Money(userInput: budgetText).map { .some($0) }
    }

    private var invalidReason: String? {
        if trimmedName == nil { return "Enter a name." }
        if budget == nil { return "The budget isn't a valid amount." }
        return nil
    }

    var body: some View {
        FormSheet(
            title: "New category",
            canSave: invalidReason == nil,
            invalidReason: invalidReason,
            save: .waitsForServer(
                { draftId in await create(draftId: draftId) },
                failure: { model.error }
            )
        ) {
            Section {
                TextField("Name", text: $name)
                Picker("Type", selection: $kind) {
                    ForEach(CategoryKind.allKnown, id: \.self) { Text($0.rawValue.capitalized).tag($0) }
                }
                TextField("Monthly budget (optional)", text: $budgetText)
                    .keyboardType(.decimalPad)
            }
        }
        .presentationDetents([.medium])
    }

    private func create(draftId: UUID) async -> Bool {
        guard let trimmedName, let budget else { return false }
        let draft = CategoryDraft(id: draftId, name: trimmedName, kind: kind, monthlyBudget: budget)
        guard let category = await model.create(draft, api: api) else { return false }
        onCreated(category)
        return true
    }
}

/// The New category sheet's request. A duplicate name is reported in plain words.
@Observable
final class NewCategoryModel {
    static let duplicateMessage = "You already have a category with that name"

    /// The latest failed create.
    private(set) var error: APIError?

    /// The created category, or nil with `error` set (nil for a cancelled create).
    func create(_ draft: CategoryDraft, api: APIClient) async -> Category? {
        error = nil
        do {
            return try await api.createCategory(draft)
        } catch is CancellationError {
            return nil
        } catch {
            self.error = Self.report(APIError(wrapping: error))
            return nil
        }
    }

    /// `error`, with a 409 (the name is taken) worded for the user.
    static func report(_ error: APIError) -> APIError {
        guard case .server(let server) = error, server.status == 409 else { return error }
        return .server(.init(status: 409, code: server.code, message: duplicateMessage, requestId: server.requestId))
    }
}
