import SwiftUI

private struct InboxEditedKey: EnvironmentKey {
    static let defaultValue: (() -> Void)? = nil
}

extension EnvironmentValues {
    /// Called after a change from a pushed screen touched transactions, so the Inbox badge and lists catch up.
    var inboxEdited: (() -> Void)? {
        get { self[InboxEditedKey.self] }
        set { self[InboxEditedKey.self] = newValue }
    }
}

/// A change the assistant proposes, under the answer it came with. What the server counted leads, then the steps;
/// the assistant's own summary comes last because the text it writes can echo hostile bank or merchant text.
struct ActionCard: View {
    let action: AIAction
    let categories: [Category]
    let isBusy: Bool
    let notice: String?
    let onConfirm: () -> Void
    let onDismiss: () -> Void
    let onUndo: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                ForEach(action.lines, id: \.self) { line in
                    Text(line).font(.subheadline.weight(.semibold))
                }
                if action.lines.isEmpty {
                    Text("No changes counted").font(.subheadline.weight(.semibold))
                }
            }
            DisclosureGroup("Steps") {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(Array(action.steps.enumerated()), id: \.offset) { _, step in
                        Text(Self.label(for: step, categories: categories, steps: action.steps))
                            .font(.footnote)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .padding(.top, 4)
            }
            .font(.footnote)
            Text(ChatView.markdown(action.summary))
                .font(.footnote)
                .foregroundStyle(.secondary)
            if let notice {
                Text(notice).font(.footnote).foregroundStyle(.red)
            }
            footer
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.secondarySystemBackground), in: .rect(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Color.accentColor.opacity(0.35)))
        .padding(.trailing, 24)
        .accessibilityElement(children: .contain)
    }

    @ViewBuilder
    private var footer: some View {
        switch action.status {
        case .proposed:
            HStack {
                if !action.lines.isEmpty {
                    Button("Confirm", action: onConfirm)
                        .buttonStyle(.borderedProminent)
                        .accessibilityLabel("Confirm: \(action.summary)")
                }
                Button("Not now", action: onDismiss)
                    .buttonStyle(.bordered)
                    .accessibilityLabel("Not now: \(action.summary)")
            }
            .disabled(isBusy)
        case .applied:
            HStack {
                Label("Applied", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                Spacer()
                Button("Undo", action: onUndo)
                    .buttonStyle(.bordered)
                    .accessibilityLabel("Undo: \(action.summary)")
                    .disabled(isBusy)
            }
            .font(.subheadline)
        case .undone:
            Label("Undone", systemImage: "arrow.uturn.backward.circle").font(.subheadline).foregroundStyle(.secondary)
        case .dismissed:
            Label("Dismissed", systemImage: "xmark.circle").font(.subheadline).foregroundStyle(.secondary)
        case .failed:
            Label(action.error ?? "Couldn't apply this", systemImage: "exclamationmark.triangle")
                .font(.subheadline)
                .foregroundStyle(.red)
        case .other:
            EmptyView()
        }
    }

    /// One step as a line: its kind and the name or count it carries. `steps` are the action's
    /// steps, so a category an earlier step creates still has a name.
    static func label(for step: AIActionStep, categories: [Category], steps: [AIActionStep] = []) -> String {
        let count = "\(step.transactionCount) \(step.transactionCount == 1 ? "transaction" : "transactions")"
        let name = step.name ?? "(unnamed)"
        switch step.kind {
        case .categorizeTransactions:
            if let category = categories.first(where: { $0.id == step.categoryId }) {
                return "Categorize \(count) as \(category.name)"
            }
            return "Categorize \(count)"
        case .markReviewed: return "Mark \(count) reviewed"
        case .createCategory: return "Create category \(name)"
        case .updateCategory: return categoryChange(step, categories: categories, steps: steps)
        case .createRule: return "Create rule for \(name)"
        case .createSubscription: return "Add subscription \(name)"
        case .updateSubscription: return "Change subscription \(name)"
        case .other: return "Other change"
        }
    }

    /// An update_category step under the category's current name, e.g. "Set Groceries budget to $250.00".
    private static func categoryChange(_ step: AIActionStep, categories: [Category], steps: [AIActionStep]) -> String {
        let current = categories.first(where: { $0.id == step.id })?.name
            ?? steps.first(where: { $0.createdCategoryId == step.id })?.name
            ?? "a category"
        if step.changesOnlyBudget {
            return step.monthlyBudget.map { "Set \(current) budget to \($0.formatted())" } ?? "Clear \(current) budget"
        }
        var changes: [String] = []
        if step.patchFields.contains("name"), let name = step.name { changes.append("rename to \(name)") }
        if step.patchFields.contains("monthlyBudget") {
            changes.append(step.monthlyBudget.map { "budget \($0.formatted())" } ?? "clear budget")
        }
        if let archived = step.archived { changes.append(archived ? "archive" : "unarchive") }
        guard !changes.isEmpty else { return "Change category \(current)" }
        return "Change category \(current): \(changes.joined(separator: ", "))"
    }
}
