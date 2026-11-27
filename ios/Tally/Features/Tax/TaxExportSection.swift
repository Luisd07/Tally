import SwiftUI

/// The Tax screen's export row. Failures show as an alert; the zip is deleted when the share sheet's activity
/// completes.
struct TaxExportSection: View {
    let year: Int
    let model: ExportModel
    @Environment(APIClient.self) private var api

    var body: some View {
        Section {
            Button {
                Task {
                    await model.export(named: ExportFile.taxZipName(year: year)) { try await api.taxExport(year: year) }
                }
            } label: {
                HStack {
                    Label("Export \(String(year))", systemImage: "square.and.arrow.up")
                    Spacer()
                    if model.isExporting { ProgressView() }
                }
            }
            .disabled(model.isExporting)
        } footer: {
            Text("A zip with the year's transactions, receipts and a list of what's missing.")
        }
        .exportSharing(model)
    }
}
