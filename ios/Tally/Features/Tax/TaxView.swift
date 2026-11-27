import SwiftUI

/// The Tax screen: a year, and that year's sections. `year` is the screen's, so a section beside the missing
/// receipts can follow it.
struct TaxView: View {
    @Environment(APIClient.self) private var api
    @State private var year = MonthRef().year
    @State private var summary = KeyedValue<Int, TaxSummary>()
    @State private var missingReceipts: MissingReceiptsModel
    @State private var uploads: ReceiptUploads
    @State private var reference = ReferenceData()
    @State private var taxExport = ExportModel(kind: .taxZip)
    @State private var attachTarget: Transaction?
    @State private var taxReview = TaxReviewModel()
    @State private var taxReviewRequests = 0
    /// Whether the server has AI assist on, so the AI review shows (#175).
    @State private var aiEnabled = false

    init() {
        let missingReceipts = MissingReceiptsModel()
        _missingReceipts = State(initialValue: missingReceipts)
        _uploads = State(initialValue: ReceiptUploads { [weak missingReceipts] in missingReceipts?.receiptAttached($0) })
    }

    var body: some View {
        List {
            ReferenceDataRetrySection(reference: reference)
            TaxExportSection(year: year, model: taxExport)
            TaxSummarySection(year: year, summary: summary) { Task { await loadSummary() } }
            if aiEnabled {
                TaxReviewSection(year: year, model: taxReview, requests: $taxReviewRequests)
            }
            MissingReceiptsSection(year: year, model: missingReceipts, categories: reference.categories) {
                attachTarget = $0
            }
        }
        .navigationTitle("Tax")
        .navigationBarTitleDisplayMode(.inline)
        .safeAreaInset(edge: .top) { YearSwitcher(year: $year) }
        .attachesReceipts(to: $attachTarget, uploads: uploads)
        .task(id: year) { await loadSummary() }
        .task(id: missingReceipts.filter(year: year)) { await missingReceipts.load(year: year, api: api) }
        .task { await reference.load(api: api) }
        .task { aiEnabled = (try? await api.aiUsage())?.enabled ?? false }
        .task(id: "\(year)-\(taxReviewRequests)-\(aiEnabled)") {
            if aiEnabled { await taxReview.follow(year: year, api: api) }
        }
        .refreshable { await reload() }
        .reloadOnReturnToForeground { await reload() }
    }

    private func reload() async {
        async let loadedSummary: Void = loadSummary()
        await missingReceipts.load(year: year, api: api)
        await loadedSummary
    }

    private func loadSummary() async {
        await summary.load(key: year) { try await api.taxSummary(year: year) }
    }
}
