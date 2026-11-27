import SwiftUI

extension View {
    /// Presents the share sheet while `model` has a file ready, and alerts when an export fails.
    func exportSharing(_ model: ExportModel) -> some View {
        sheet(
            item: Binding(
                get: { model.readyURL.map(ReadyExport.init) },
                set: { if $0 == nil { model.sheetDismissed() } }
            )
        ) { export in
            ShareSheet(items: [export.url], onDone: model.finish).presentationDetents([.medium, .large])
        }
        .failureAlert("Couldn't export", message: model.failureMessage, clear: model.clearFailure)
    }
}

private struct ReadyExport: Identifiable {
    let url: URL
    var id: URL { url }
}
