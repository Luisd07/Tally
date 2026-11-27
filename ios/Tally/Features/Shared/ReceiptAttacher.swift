import PhotosUI
import SwiftUI

extension View {
    /// Setting `target` asks for that transaction's receipt (and clears `target`): a document scan or a photo from
    /// the library, or straight to the library where there's no document camera (the simulator). The receipt is
    /// uploaded through `uploads`, whose banner sits at the bottom of this view.
    func attachesReceipts(to target: Binding<Transaction?>, uploads: ReceiptUploads) -> some View {
        modifier(ReceiptAttacher(target: target, uploads: uploads))
    }
}

private struct ReceiptAttacher: ViewModifier {
    @Binding var target: Transaction?
    let uploads: ReceiptUploads

    @Environment(APIClient.self) private var api
    /// The transaction the latest scanner or picker was opened for.
    @State private var picking: Transaction?
    @State private var showsSourceChoice = false
    @State private var showsScanner = false
    @State private var showsPicker = false
    @State private var photo: PhotosPickerItem?

    func body(content: Content) -> some View {
        content
            .safeAreaInset(edge: .bottom) { ReceiptUploadBanner(uploads: uploads) }
            .confirmationDialog("Attach receipt", isPresented: $showsSourceChoice, titleVisibility: .visible) {
                Button("Scan receipt") { showsScanner = true }
                Button("Choose from library") { showsPicker = true }
            }
            .receiptScanner(isPresented: $showsScanner, onScan: upload(scannedPages:)) {
                guard let transaction = picking else { return }
                uploads.scanFailed(for: transaction.id, name: transaction.displayName)
            }
            .photosPicker(isPresented: $showsPicker, selection: $photo, matching: .images)
            .onChange(of: target) { _, transaction in
                guard let transaction else { return }
                picking = transaction
                target = nil
                if ReceiptScanner.isSupported {
                    showsSourceChoice = true
                } else {
                    showsPicker = true
                }
            }
            .onChange(of: photo) { _, picked in
                guard let picked, let transaction = picking else { return }
                photo = nil
                Task {
                    await uploads.attach(to: transaction.id, name: transaction.displayName, api: api) {
                        try? await picked.loadTransferable(type: Data.self)
                    }
                }
            }
    }

    private func upload(scannedPages pages: [CGImage]) {
        guard let transaction = picking else { return }
        Task { await uploads.attach(to: transaction.id, name: transaction.displayName, api: api, scannedPages: pages) }
    }
}
