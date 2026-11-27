import SwiftUI

/// Progress, failure and retry for receipt uploads. Sits outside any list, so it shows with an empty Inbox too.
struct ReceiptUploadBanner: View {
    let uploads: ReceiptUploads

    @Environment(APIClient.self) private var api

    var body: some View {
        if !uploads.uploads.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(uploads.uploads) { upload in
                    row(upload)
                }
            }
            .padding()
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.regularMaterial)
        }
    }

    @ViewBuilder
    private func row(_ upload: ReceiptUploads.Upload) -> some View {
        switch upload.state {
        case .uploading:
            HStack {
                ProgressView()
                Text("Uploading receipt for \(upload.name)…")
            }
        case .done:
            Label("Receipt attached to \(upload.name)", systemImage: "checkmark.circle").foregroundStyle(.green)
        case .failed(let message):
            VStack(alignment: .leading, spacing: 4) {
                Label("\(upload.name): \(message)", systemImage: "exclamationmark.triangle").foregroundStyle(.red)
                HStack {
                    if upload.canRetry {
                        Button("Retry upload") { Task { await uploads.retry(upload.id, api: api) } }
                    }
                    Button("Dismiss") { uploads.dismiss(upload.id) }
                }
                .buttonStyle(.bordered)
            }
        }
    }
}
