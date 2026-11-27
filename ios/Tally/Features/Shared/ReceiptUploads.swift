import CoreGraphics
import Foundation
import Observation

/// Receipt uploads in flight or just finished, one per transaction. Nothing is persisted: a retry
/// payload lives in memory until the upload succeeds or is dismissed.
@Observable
final class ReceiptUploads {
    enum State: Equatable {
        case uploading, done, failed(String)
    }

    struct Upload: Identifiable {
        let transactionId: UUID
        /// What the banner calls the transaction.
        let name: String
        fileprivate(set) var state: State
        fileprivate let payload: Payload?

        var id: UUID { transactionId }
        var canRetry: Bool { payload != nil }
    }

    /// A converted photo or scan; `receiptId` is minted once per picked photo or scan so a retry replays instead of duplicating.
    fileprivate struct Payload {
        let jpeg: Data
        let receiptId: UUID
    }

    /// In the order the uploads started.
    private(set) var uploads: [Upload] = []
    /// How many uploads the server has accepted, so a screen can reload its receipts after each.
    private(set) var completedCount = 0

    private let doneDisplay: Duration
    /// Called with the transaction's id after each upload the server accepted, a retry's included.
    private let onUploaded: ((UUID) -> Void)?

    init(doneDisplay: Duration = .seconds(3), onUploaded: ((UUID) -> Void)? = nil) {
        self.doneDisplay = doneDisplay
        self.onUploaded = onUploaded
    }

    /// Reads the picked photo with `loadData`, converts it to JPEG off the main actor and uploads it.
    /// A new photo for a transaction replaces that transaction's earlier entry.
    func attach(to transactionId: UUID, name: String, api: APIClient, loadData: () async -> Data?) async {
        await attach(to: transactionId, name: name, api: api) {
            guard let data = await loadData() else { return .failed("Couldn't read the photo.") }
            let jpeg = await Task.detached { ReceiptImage.jpeg(from: data) }.value
            return jpeg.map(Prepared.jpeg) ?? .failed("That photo couldn't be converted to JPEG.")
        }
    }

    /// Stacks the document camera's pages into one JPEG off the main actor and uploads it.
    func attach(to transactionId: UUID, name: String, api: APIClient, scannedPages pages: [CGImage]) async {
        await attach(to: transactionId, name: name, api: api) {
            let jpeg = await Task.detached { ReceiptImage.jpeg(stacking: pages) }.value
            return jpeg.map(Prepared.jpeg) ?? .failed("Couldn't read the scan.")
        }
    }

    /// Shows a failed entry for a scan the document camera couldn't finish; there's nothing to retry.
    func scanFailed(for transactionId: UUID, name: String) {
        set(Upload(transactionId: transactionId, name: name, state: .failed("The scan didn't finish. Try again."), payload: nil))
    }

    private enum Prepared {
        case jpeg(Data), failed(String)
    }

    private func attach(to transactionId: UUID, name: String, api: APIClient, prepare: () async -> Prepared) async {
        set(Upload(transactionId: transactionId, name: name, state: .uploading, payload: nil))
        switch await prepare() {
        case .failed(let message):
            fail(transactionId, message)
        case .jpeg(let jpeg):
            let payload = Payload(jpeg: jpeg, receiptId: UUID())
            set(Upload(transactionId: transactionId, name: name, state: .uploading, payload: payload))
            await send(payload, for: transactionId, api: api)
        }
    }

    /// Sends a failed upload again with its original `receiptId`.
    func retry(_ transactionId: UUID, api: APIClient) async {
        guard let upload = uploads.first(where: { $0.id == transactionId }),
              case .failed = upload.state, let payload = upload.payload else { return }
        set(Upload(transactionId: transactionId, name: upload.name, state: .uploading, payload: payload))
        await send(payload, for: transactionId, api: api)
    }

    func dismiss(_ transactionId: UUID) {
        uploads.removeAll { $0.id == transactionId }
    }

    private func send(_ payload: Payload, for transactionId: UUID, api: APIClient) async {
        do {
            _ = try await api.uploadReceipt(transactionId: transactionId, jpeg: payload.jpeg, receiptId: payload.receiptId)
        } catch {
            update(transactionId, state: .failed(error.localizedDescription))
            return
        }
        set(Upload(transactionId: transactionId, name: name(of: transactionId), state: .done, payload: nil))
        completedCount += 1
        onUploaded?(transactionId)
        Task {
            try? await Task.sleep(for: doneDisplay)
            if uploads.first(where: { $0.id == transactionId })?.state == .done { dismiss(transactionId) }
        }
    }

    private func name(of transactionId: UUID) -> String {
        uploads.first { $0.id == transactionId }?.name ?? ""
    }

    private func set(_ upload: Upload) {
        if let index = uploads.firstIndex(where: { $0.id == upload.id }) {
            uploads[index] = upload
        } else {
            uploads.append(upload)
        }
    }

    private func update(_ transactionId: UUID, state: State) {
        guard let index = uploads.firstIndex(where: { $0.id == transactionId }) else { return }
        uploads[index].state = state
    }

    private func fail(_ transactionId: UUID, _ message: String) {
        update(transactionId, state: .failed(message))
    }
}
