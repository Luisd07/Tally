import SwiftUI

struct CategoriesView: View {
    @Environment(APIClient.self) private var api
    @Environment(\.theme) private var theme
    @State private var model = CategoriesModel()
    @State private var sheet: CategorySheet?
    @State private var showsArchived = false
    @State private var archiveFailure: String?

    var body: some View {
        content
            .navigationTitle("Categories")
            .navigationBarTitleDisplayMode(.inline)
            .task { await load() }
            .refreshable { await load() }
            .reloadOnReturnToForeground { await load() }
            .sheet(item: $sheet) { sheet in
                switch sheet {
                case .add:
                    NewCategorySheet(kind: .expense) { model.add($0) }
                case .edit(let category):
                    EditCategorySheet(category: category, model: model)
                }
            }
            .failureAlert("Couldn't save", message: archiveFailure) { archiveFailure = nil }
    }

    @ViewBuilder
    private var content: some View {
        if model.hasData {
            list
        } else if let error = model.error {
            LoadFailedView(title: "Couldn't load Categories", error: error) { Task { await load() } }
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
                Text("Categories group your spending. Budgets and the dashboard use them.")
                    .foregroundStyle(.secondary)
                Button("New category", systemImage: "plus.circle.fill") { sheet = .add }
            }
            if !model.expense.isEmpty { rowsSection("Expense", rows: model.expense) }
            if !model.income.isEmpty { rowsSection("Income", rows: model.income) }
            if !model.archived.isEmpty {
                Section {
                    DisclosureGroup("Archived (\(model.archived.count))", isExpanded: $showsArchived) {
                        ForEach(model.archived) { row($0) }
                    }
                }
            }
        }
        .themedList(theme)
    }

    private func rowsSection(_ title: String, rows: [Category]) -> some View {
        Section(title) {
            ForEach(rows) { row($0) }
        }
    }

    private func row(_ category: Category) -> some View {
        EditRowButton { sheet = .edit(category) } label: {
            HStack {
                Text(category.name)
                Spacer()
                if let budget = category.monthlyBudget {
                    HStack(spacing: 2) {
                        AmountText(budget, role: .neutral)
                        Text("/ mo")
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
            }
        }
        .swipeActions(edge: .trailing) {
            if category.archived {
                Button("Unarchive", systemImage: "tray.and.arrow.up") { setArchived(category, to: false) }
                    .tint(.accentColor)
            } else {
                Button("Archive", systemImage: "archivebox") { setArchived(category, to: true) }
                    .tint(.gray)
            }
        }
    }

    private func setArchived(_ category: Category, to archived: Bool) {
        Task {
            if await model.setArchived(category, to: archived, api: api) { return }
            archiveFailure = model.updateError.map { "\(category.name): \($0.localizedDescription)" }
        }
    }

    private func load() async {
        await model.load(api: api)
    }
}

private enum CategorySheet: Identifiable {
    case add
    case edit(Category)

    var id: String {
        switch self {
        case .add: "add"
        case .edit(let category): category.id.uuidString
        }
    }
}

/// Renames a category, changes its kind or sets or clears its monthly budget. Save waits for the server, so a
/// duplicate name shows in the sheet.
private struct EditCategorySheet: View {
    let category: Category
    let model: CategoriesModel

    @Environment(APIClient.self) private var api
    @State private var name: String
    @State private var kind: CategoryKind
    @State private var budgetText: String

    init(category: Category, model: CategoriesModel) {
        self.category = category
        self.model = model
        _name = State(initialValue: category.name)
        _kind = State(initialValue: category.kind)
        _budgetText = State(initialValue: category.monthlyBudget?.inputText ?? "")
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
            title: "Edit category",
            canSave: invalidReason == nil,
            invalidReason: invalidReason,
            save: .waitsForServer(
                { _ in await save() },
                failure: { model.updateError.map(NewCategoryModel.report) }
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

    private func save() async -> Bool {
        guard let trimmedName, let budget else { return false }
        let patch = CategoryPatch(
            name: trimmedName == category.name ? nil : trimmedName,
            kind: kind == category.kind ? nil : kind,
            monthlyBudget: budget == category.monthlyBudget ? nil : .some(budget)
        )
        return await model.update(category, patch: patch, api: api)
    }
}
