import SwiftUI

private struct ShowCategoryTransactionsKey: EnvironmentKey {
    static let defaultValue: ((TransactionFocus) -> Void)? = nil
}

extension EnvironmentValues {
    /// Switches to the Transactions tab filtered to one category and month.
    var showCategoryTransactions: ((TransactionFocus) -> Void)? {
        get { self[ShowCategoryTransactionsKey.self] }
        set { self[ShowCategoryTransactionsKey.self] = newValue }
    }
}

struct TransactionsView: View {
    @Environment(APIClient.self) private var api
    @State private var model: TransactionsModel
    @State private var searchText = ""
    @State private var reference = ReferenceData()
    @State private var bulkCategorizing = false
    @State private var csvExport = ExportModel(kind: .transactionsCSV)

    @Binding private var focus: TransactionFocus?

    private let currentMonth = MonthRef()
    private let isSelected: Bool

    /// `isSelected` is true while this tab is the visible one; becoming selected reloads the list.
    /// `onEdited` is called after an edit saved, so the Inbox badge can catch up.
    /// `focus` is a category and month another tab wants shown; the view applies it and clears it.
    init(isSelected: Bool, focus: Binding<TransactionFocus?>, onEdited: @escaping () -> Void) {
        self.isSelected = isSelected
        _focus = focus
        _model = State(initialValue: TransactionsModel(onEdited: onEdited))
    }

    var body: some View {
        NavigationStack {
            content
                .navigationDestination(for: UUID.self) { id in
                    TransactionDetailView(
                        id: id, model: model, categories: reference.categories, accounts: reference.accounts
                    )
                    .environment(\.onCategoryCreated, addCategory)
                }
                .navigationTitle("Transactions")
                .environment(\.onCategoryCreated, addCategory)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    MonthSwitcher(month: $model.filter.month, latest: currentMonth).toolbar
                    ToolbarItem(placement: .topBarTrailing) { filterMenu }
                }
                .searchable(text: $searchText, prompt: "Search transactions")
                .onSubmit(of: .search) { model.filter.search = searchText }
                .task(id: searchText) {
                    try? await Task.sleep(for: .milliseconds(300))
                    guard !Task.isCancelled else { return }
                    model.filter.search = searchText
                }
                .onChange(of: focus, initial: true) { _, requested in
                    guard let requested else { return }
                    focus = nil
                    searchText = ""
                    model.filter.show(requested)
                }
                .task(id: model.filter) { await model.loadIfNeeded(api: api) }
                .task { if reference.categories.isEmpty { await reference.load(api: api) } }
                .onChange(of: isSelected) { _, selected in
                    if selected { Task { await refresh() } }
                }
                .refreshable { await refresh() }
                .reloadOnReturnToForeground { await refresh() }
        }
        .editFailureAlert(model.editError, clear: model.clearEditError)
        .exportSharing(csvExport)
    }

    @ViewBuilder
    private var content: some View {
        if !model.items.isEmpty {
            list
        } else if let error = model.error {
            LoadFailedView(title: "Couldn't load transactions", error: error) { Task { await reload() } }
        } else if model.hasLoaded {
            emptyState
        } else {
            ProgressView()
        }
    }

    private var list: some View {
        List {
            if let error = model.error {
                RetrySection(title: "Couldn't refresh", error: error) { Task { await reload() } }
            }
            ReferenceDataRetrySection(reference: reference)
            ForEach(model.items) { transaction in
                row(transaction)
                    .loadsMore(whenLast: transaction.id == model.items.last?.id, hasMore: model.hasMore) {
                        Task { await model.loadMore(api: api) }
                    }
            }
            if model.isLoadingMore { LoadingMoreRow() }
        }
        .safeAreaInset(edge: .top) { selectionHeader }
        .safeAreaInset(edge: .bottom) {
            if model.isSelecting { bulkActions }
        }
        .sheet(isPresented: $bulkCategorizing) {
            CategoryEditorSheet(categoryId: model.sharedCategoryId, categories: reference.categories) { categoryId in
                Task { await model.bulkSetCategory(categoryId, api: api) }
            }
        }
    }

    /// Lists a category made from a category editor, so the rows show its name at once.
    private func addCategory(_ category: Category) {
        if !reference.categories.contains(where: { $0.id == category.id }) { reference.add(category) }
    }

    /// Opens the detail, or while selecting, picks or unpicks the row.
    @ViewBuilder
    private func row(_ transaction: Transaction) -> some View {
        let content = TransactionRow(
            transaction: transaction,
            categories: reference.categories,
            statuses: TransactionStatus.of(transaction, in: .transactions)
        )
        if model.isSelecting {
            let selected = model.isInSelection(transaction.id)
            Button {
                model.toggleSelection(transaction.id)
            } label: {
                HStack {
                    Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                        .foregroundStyle(selected ? Color.accentColor : .secondary)
                    content
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityAddTraits(selected ? .isSelected : [])
        } else {
            NavigationLink(value: transaction.id) { content }
        }
    }

    /// "Select" to start picking rows; while picking, the count, "Select all" and "Done".
    private var selectionHeader: some View {
        HStack {
            if model.isSelecting {
                VStack(alignment: .leading) {
                    Text("\(model.selection.count) selected")
                    if model.isSelectionFull {
                        Text("Up to \(TransactionBulkUpdate.maxIds) at a time")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                Spacer()
                Button("Select all") { model.selectAllLoaded() }
                    .disabled(!model.canSelectAll)
                Button("Done") { model.endSelecting() }
                    .bold()
            } else {
                Button {
                    shareCSV()
                } label: {
                    HStack {
                        Label("Share CSV", systemImage: "square.and.arrow.up")
                        if csvExport.isExporting { ProgressView() }
                    }
                }
                .disabled(csvExport.isExporting)
                Spacer()
                Button("Select") { model.beginSelecting() }
            }
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(.bar)
    }

    /// One change for every selected row.
    private var bulkActions: some View {
        HStack {
            Button("Category", systemImage: "tag") { bulkCategorizing = true }
            Spacer()
            Menu {
                ForEach(Bucket.allKnown, id: \.rawValue) { bucket in
                    Button(bucket.label) { Task { await model.bulkSetBucket(bucket, api: api) } }
                }
            } label: {
                Label("Bucket", systemImage: "tray")
            }
            Spacer()
            Button("Reviewed", systemImage: "checkmark.circle") { Task { await model.bulkSetReviewed(true, api: api) } }
            Spacer()
            Button("Unreviewed", systemImage: "circle") { Task { await model.bulkSetReviewed(false, api: api) } }
        }
        .labelStyle(.titleOnly)
        .font(.subheadline)
        .padding()
        .background(.bar)
        .disabled(model.selection.isEmpty)
    }

    @ViewBuilder
    private var emptyState: some View {
        if model.filter.search.isEmpty {
            ContentUnavailableView(
                "No transactions",
                systemImage: "tray",
                description: Text(model.filter.hasRefinements ? "Nothing matches the filters." : "Nothing in this month.")
            )
        } else {
            ContentUnavailableView.search(text: model.filter.search)
        }
    }

    private var filterMenu: some View {
        Menu {
            bucketPicker
            accountPicker
            categoryPicker
            if model.filter.hasRefinements {
                Button("Clear filters", systemImage: "xmark.circle") { model.filter.clearRefinements() }
            }
        } label: {
            Label(
                "Filters",
                systemImage: model.filter.hasRefinements
                    ? "line.3.horizontal.decrease.circle.fill"
                    : "line.3.horizontal.decrease.circle"
            )
        }
    }

    private var bucketPicker: some View {
        Picker("Bucket", selection: $model.filter.bucket) {
            Text("All buckets").tag(Bucket?.none)
            ForEach(Bucket.allKnown, id: \.rawValue) { bucket in
                Text(bucket.label).tag(Bucket?.some(bucket))
            }
        }
    }

    private var accountPicker: some View {
        Picker("Account", selection: $model.filter.accountId) {
            Text("All accounts").tag(UUID?.none)
            ForEach(reference.accounts) { account in
                Text(account.name).tag(UUID?.some(account.id))
            }
        }
    }

    private var categoryPicker: some View {
        Picker("Category", selection: $model.filter.categoryId) {
            Text("All categories").tag(UUID?.none)
            ForEach(reference.categories) { category in
                Text(category.archived ? "\(category.name) (archived)" : category.name).tag(UUID?.some(category.id))
            }
        }
    }

    /// Exports every row of the current filter, not just the loaded pages.
    private func shareCSV() {
        let filter = model.filter
        Task {
            await csvExport.export(named: ExportFile.transactionsCSVName(month: filter.month)) {
                try await api.transactionsCSV(filter.query())
            }
        }
    }

    private func reload() async {
        await model.load(api: api)
    }

    private func refresh() async {
        async let refreshed: Void = reload()
        async let named: Void = reference.load(api: api)
        _ = await (refreshed, named)
    }
}
