import SwiftUI

struct RulesView: View {
    @Environment(APIClient.self) private var api
    @State private var model = RulesModel()
    @State private var reference = ReferenceData()

    var body: some View {
        content
            .navigationTitle("Rules")
            .navigationBarTitleDisplayMode(.inline)
            .task {
                async let referenceLoad: Void = reference.load(api: api)
                await load()
                await referenceLoad
            }
            .refreshable { await load() }
            .reloadOnReturnToForeground { await load() }
            .editFailureAlert(model.editError, clear: model.clearEditError)
    }

    @ViewBuilder
    private var content: some View {
        if model.hasData {
            list
        } else if let error = model.error {
            LoadFailedView(title: "Couldn't load Rules", error: error) { Task { await load() } }
        } else {
            ProgressView().frame(maxHeight: .infinity)
        }
    }

    private var list: some View {
        List {
            if let error = model.error {
                RetrySection(title: "Couldn't refresh", error: error) { Task { await load() } }
            }
            ReferenceDataRetrySection(reference: reference)
            Section {
                Text("Merchants that always go to the same category. They apply to new bank transactions.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            Section {
                if model.rules.isEmpty {
                    Text("No rules yet. Correct a transaction to add one.").foregroundStyle(.secondary)
                }
                ForEach(model.rules) { rule in
                    RuleRow(rule: rule, categories: reference.categories, isActive: activeBinding(rule))
                        .swipeActions(edge: .trailing) {
                            Button("Delete", role: .destructive) { Task { await model.delete(rule, api: api) } }
                        }
                }
            }
        }
    }

    private func activeBinding(_ rule: Rule) -> Binding<Bool> {
        Binding(
            get: { rule.active },
            set: { active in Task { await model.setActive(rule, to: active, api: api) } }
        )
    }

    private func load() async {
        await model.load(api: api)
    }
}

/// A rule's pattern and match type, what it sets, and its on/off switch.
struct RuleRow: View {
    let rule: Rule
    let categories: [Category]
    let isActive: Binding<Bool>

    var body: some View {
        Toggle(isOn: isActive) {
            VStack(alignment: .leading, spacing: 4) {
                Text(rule.pattern)
                Text(rule.matchType.label).font(.caption).foregroundStyle(.secondary)
                Text(rule.targetSummary(categories: categories)).font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

extension Rule {
    /// "→ Hertz · Travel · Reimbursable": the merchant, category and bucket it sets, by `targetLabels`. A rule that
    /// sets nothing lost its only target when its category was deleted, so it reads "→ Deleted category".
    func targetSummary(categories: [Category]) -> String {
        let parts = [setMerchant].compactMap { $0 }
            + Rule.targetLabels(categoryId: setCategoryId, bucket: setBucket, categories: categories)
        return "→ " + (parts.isEmpty ? "Deleted category" : parts.joined(separator: " · "))
    }
}
