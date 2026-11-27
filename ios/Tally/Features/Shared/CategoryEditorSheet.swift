import SwiftUI

/// Edits a transaction's category and bucket. Without categories (not loaded) the category is left
/// as it is and only the bucket can change. A bucket the app doesn't know stays selectable, so
/// saving without touching it sends the same string back. The category-only form, for several
/// transactions at once, can't save until the categories have loaded.
struct CategoryEditorSheet: View {
    let categories: [Category]

    @Environment(\.dismiss) private var dismiss
    @Environment(\.onCategoryCreated) private var onCategoryCreated
    @State private var categoryId: UUID?
    /// Categories made from this sheet that `categories` doesn't have yet; the screen adds them through
    /// `onCategoryCreated`, so they can reach `categories` while the sheet is open.
    @State private var created: [Category] = []
    @State private var isCreating = false
    /// The picked bucket; nil in the category-only form, which has no bucket.
    @State private var bucket: Bucket?
    private let mode: Mode

    private enum Mode {
        /// A transaction's category and bucket; `original` is its bucket.
        case transaction(original: Bucket, onSave: (UUID?, Bucket) -> Void)
        /// Only a category.
        case categoryOnly(onSave: (UUID?) -> Void)
    }

    init(transaction: Transaction, categories: [Category], onSave: @escaping (UUID?, Bucket) -> Void) {
        self.categories = categories
        mode = .transaction(original: transaction.bucket, onSave: onSave)
        _categoryId = State(initialValue: transaction.categoryId)
        _bucket = State(initialValue: transaction.bucket)
    }

    /// Picks only a category, starting from `categoryId`.
    init(categoryId: UUID?, categories: [Category], onSave: @escaping (UUID?) -> Void) {
        self.categories = categories
        mode = .categoryOnly(onSave: onSave)
        _categoryId = State(initialValue: categoryId)
    }

    var body: some View {
        NavigationStack {
            Form {
                if categories.isEmpty {
                    Text(notLoadedMessage)
                        .foregroundStyle(.secondary)
                } else {
                    Picker("Category", selection: $categoryId) {
                        Text("None").tag(UUID?.none)
                        ForEach(pickableCategories) { category in
                            Text(category.name).tag(Optional(category.id))
                        }
                    }
                    Button("New category…", systemImage: "plus") { isCreating = true }
                }
                if case .transaction(let original, _) = mode {
                    Picker("Bucket", selection: $bucket) {
                        ForEach(Bucket.pickerOptions(including: original), id: \.self) {
                            Text($0.label).tag(Optional($0))
                        }
                    }
                }
            }
            .sheet(isPresented: $isCreating) {
                NewCategorySheet(kind: newCategoryKind) { category in
                    created.append(category)
                    onCategoryCreated(category)
                    categoryId = category.id
                }
            }
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        save()
                        dismiss()
                    }
                    .disabled(!canSave)
                }
            }
        }
        .presentationDetents([.medium])
    }

    private var pickableCategories: [Category] {
        (categories + created.filter { new in !categories.contains { $0.id == new.id } }).pickable(keeping: categoryId)
    }

    private var title: String {
        switch mode {
        case .transaction: "Recategorize"
        case .categoryOnly: "Category"
        }
    }

    /// The kind a new category starts as: income for a transaction in the income bucket, otherwise expense.
    private var newCategoryKind: CategoryKind {
        bucket == .income ? .income : .expense
    }

    private var notLoadedMessage: String {
        switch mode {
        case .transaction: "Categories haven't loaded, so only the bucket can change."
        case .categoryOnly: "Categories haven't loaded yet."
        }
    }

    /// The category-only form has nothing to save until the categories load.
    private var canSave: Bool {
        switch mode {
        case .transaction: true
        case .categoryOnly: !categories.isEmpty
        }
    }

    private func save() {
        switch mode {
        case .transaction(let original, let onSave): onSave(categoryId, bucket ?? original)
        case .categoryOnly(let onSave): onSave(categoryId)
        }
    }
}

private struct OnCategoryCreatedKey: EnvironmentKey {
    static let defaultValue: (Category) -> Void = { _ in }
}

extension EnvironmentValues {
    /// Called with a category made from a `CategoryEditorSheet`, so the screen that opened it can add it to its list.
    var onCategoryCreated: (Category) -> Void {
        get { self[OnCategoryCreatedKey.self] }
        set { self[OnCategoryCreatedKey.self] = newValue }
    }
}
