import Foundation
import os

/// Why the outbox refused a request.
enum OutboxError: LocalizedError {
    /// The device storage for unsent captures could not be opened, so nothing can be saved.
    case storageUnavailable

    var errorDescription: String? {
        "Captures can't be saved on this device right now."
    }
}

/// Something the user should hear about a capture that was sent.
struct OutboxNotice: Identifiable, Hashable {
    /// The capture's id.
    let id: UUID
    /// The capture's description, so the user knows which one.
    let title: String
    let message: String
}

/// Sends the stored captures to the server and publishes what is still queued.
///
/// Delivery is exactly-once by id: the capture's phone UUID is its transaction id and its receipt id is
/// sent with the upload, so replaying either request after a lost reply returns the original row.
/// Queueing a capture, or making a failed one pending again, starts a flush on its own; the app also
/// calls `requestFlush()` on launch, on return to the foreground, after a successful request and when
/// the network comes back.
@Observable
final class Outbox {
    /// Queued items still to send, oldest first. `lastError` says why an earlier attempt didn't finish.
    private(set) var pending: [OutboxItem] = []
    /// Items the server rejected; they stay until `retry` or `discard`.
    private(set) var failed: [OutboxItem] = []
    /// Ids of stored files that can't be decoded; they stay until `discard`.
    private(set) var unreadable: [UUID] = []
    /// Things the user should know about captures that were sent but not exactly as picked. They outlive the
    /// item and stay until `dismissNotice`; they are not kept across launches.
    private(set) var notices: [OutboxNotice] = []
    private(set) var isFlushing = false
    /// Why captures can't be kept safely right now: storage that never opened, a listing that can't be read
    /// (the queue shown is then the last known one), or a write that failed during a send.
    var storageProblem: String? { unavailableReason ?? readProblem ?? writeProblem }

    private static let logger = Logger(subsystem: "com.luisd07.tally", category: "Outbox")

    private var readProblem: String?
    private var writeProblem: String?
    @ObservationIgnored private let store: OutboxStore?
    @ObservationIgnored private let unavailableReason: String?
    @ObservationIgnored private let api: APIClient
    @ObservationIgnored private let autoFlush: Bool
    @ObservationIgnored private var flushRequestedWhileRunning = false
    /// The flush most recently started by `requestFlush()`, so a test can wait for it.
    @ObservationIgnored private(set) var scheduledFlush: Task<Void, Never>?

    /// `autoFlush: false` leaves flushing to explicit `flush()` calls; tests use it to control when sending happens.
    init(store: OutboxStore, api: APIClient, autoFlush: Bool = true) {
        self.store = store
        self.unavailableReason = nil
        self.api = api
        self.autoFlush = autoFlush
        reload()
    }

    /// An outbox whose storage could not be opened: it refuses captures and reports `reason`.
    init(unavailableBecause reason: String, api: APIClient) {
        self.store = nil
        self.unavailableReason = reason
        self.api = api
        self.autoFlush = false
    }

    /// The app's outbox in `directory` (default `Application Support/Outbox`). If the storage can't be opened
    /// the outbox is unavailable: `enqueue` throws and `storageProblem` says why.
    static func makeDefault(api: APIClient, directory: URL? = nil) -> Outbox {
        do {
            return Outbox(store: try OutboxStore(directory: directory), api: api)
        } catch {
            logger.error("Outbox storage unavailable: \(error.localizedDescription, privacy: .public)")
            return Outbox(unavailableBecause: error.localizedDescription, api: api)
        }
    }

    /// Queues a capture (and its receipt JPEG) and starts sending it. Throws `OutboxError.storageUnavailable`
    /// when the outbox has no storage, or the store's error when the files can't be written.
    @discardableResult
    func enqueue(_ capture: Capture, receiptJPEG: Data? = nil) throws -> OutboxItem {
        let store = try availableStore()
        defer { reload() }
        let item = try store.enqueue(capture, receiptJPEG: receiptJPEG)
        writeProblem = nil
        requestFlush()
        return item
    }

    /// Makes a failed item pending again and starts sending it.
    func retry(_ id: UUID) throws {
        let store = try availableStore()
        defer { reload() }
        try store.clearFailure(id)
        requestFlush()
    }

    /// Deletes an item (or an unreadable file) and its receipt.
    func discard(_ id: UUID) throws {
        let store = try availableStore()
        defer { reload() }
        try store.remove(id)
    }

    func dismissNotice(_ id: OutboxNotice.ID) {
        notices.removeAll { $0.id == id }
    }

    /// Starts a flush without waiting for it. Safe to call at any time and from any trigger.
    func requestFlush() {
        guard autoFlush else { return }
        scheduledFlush = Task { await flush() }
    }

    /// Sends every pending item in enqueue order. Only one flush runs at a time; a call made while one
    /// runs returns at once and asks the running flush to make one more pass, which picks up anything queued meanwhile.
    func flush() async {
        guard store != nil else { return }
        guard !isFlushing else {
            flushRequestedWhileRunning = true
            return
        }
        isFlushing = true
        writeProblem = nil
        defer {
            flushRequestedWhileRunning = false
            isFlushing = false
            reload()
        }
        await SuppressOnSuccess.$isActive.withValue(true) {
            // Items the server answered without finishing wait for the next flush, not for another pass of this one.
            var skipped: Set<UUID> = []
            repeat {
                flushRequestedWhileRunning = false
                guard await sendPending(skipping: &skipped) else { return }
            } while flushRequestedWhileRunning
        }
    }

    private enum Outcome {
        case sent
        /// The server will never accept this item; park it with the reason and go on.
        case rejected(String)
        /// The server answered but the item isn't done (5xx, 429, unreadable reply); record why and go on.
        case skip(String)
        /// Nothing more can be done now (offline, signed out, cancelled); keep everything.
        case stop
        /// The phone's storage failed; keep everything and report it.
        case storageFailure(any Error)
    }

    private enum HTTPStatus {
        static let unauthorized = 401
        static let notFound = 404
        static let tooManyRequests = 429
    }

    private func availableStore() throws -> OutboxStore {
        guard let store else { throw OutboxError.storageUnavailable }
        return store
    }

    /// Returns false when the flush should end early.
    private func sendPending(skipping skipped: inout Set<UUID>) async -> Bool {
        guard let store else { return false }
        let items: [OutboxItem]
        do {
            items = try store.allItems().items
        } catch {
            noteUnreadable(error)
            return false
        }
        for item in items where item.failure == nil && !skipped.contains(item.id) {
            switch await send(item, store: store) {
            case .sent:
                reload()
            case .rejected(let message):
                guard persist({ try store.markFailed(item.id, message: message) }) else { return false }
                reload()
            case .skip(let message):
                skipped.insert(item.id)
                guard persist({ try store.recordAttempt(item.id, error: message) }) else { return false }
                reload()
            case .stop:
                return false
            case .storageFailure(let error):
                noteWriteFailure(error)
                return false
            }
        }
        return true
    }

    /// Runs the steps the item hasn't finished (create, link the event, upload the receipt) and then removes it.
    /// A step returns nil to go on to the next one, or the outcome that ends this item's turn.
    private func send(_ item: OutboxItem, store: OutboxStore) async -> Outcome {
        if let ended = await createRow(item, store: store) { return ended }
        if let ended = await linkEvent(item, store: store) { return ended }
        if let ended = await uploadReceipt(item, store: store) { return ended }
        return finish(item, store: store)
    }

    private func createRow(_ item: OutboxItem, store: OutboxStore) async -> Outcome? {
        guard !item.rowSent else { return nil }
        do {
            _ = try await api.createTransaction(item.capture)
        } catch {
            return Self.outcome(for: error)
        }
        return persistStep { try store.markRowSent(item.id) }
    }

    /// A 404 on the PATCH means the event, or the transaction itself, is gone. The row stands without the event,
    /// the receipt still goes up, and a notice tells the user.
    private func linkEvent(_ item: OutboxItem, store: OutboxStore) async -> Outcome? {
        guard let eventId = item.capture.eventId, !item.eventLinked else { return nil }
        do {
            _ = try await api.updateTransaction(id: item.id, patch: TransactionPatch(eventId: .some(eventId)))
        } catch {
            guard case .server(let server)? = error as? APIError, server.status == HTTPStatus.notFound else {
                return Self.outcome(for: error)
            }
            noteEventDropped(for: item)
        }
        return persistStep { try store.markEventLinked(item.id) }
    }

    private func uploadReceipt(_ item: OutboxItem, store: OutboxStore) async -> Outcome? {
        guard let receiptId = item.capture.receiptId else { return nil }
        let jpeg: Data?
        do {
            jpeg = try store.receiptJPEG(for: item.id)
        } catch {
            return .storageFailure(error)
        }
        guard let jpeg else { return .rejected("The receipt image is missing.") }
        do {
            _ = try await api.uploadReceipt(transactionId: item.id, jpeg: jpeg, receiptId: receiptId)
        } catch {
            return Self.outcome(for: error)
        }
        return nil
    }

    private func finish(_ item: OutboxItem, store: OutboxStore) -> Outcome {
        persistStep { try store.remove(item.id) } ?? .sent
    }

    /// Runs a store write for a send step: nil on success, `.storageFailure` otherwise.
    private func persistStep(_ write: () throws -> Void) -> Outcome? {
        do {
            try write()
            return nil
        } catch {
            return .storageFailure(error)
        }
    }

    private func noteEventDropped(for item: OutboxItem) {
        Self.logger.info("Event link dropped for \(item.id.uuidString, privacy: .public): 404 on the PATCH")
        guard !notices.contains(where: { $0.id == item.id }) else { return }
        notices.append(OutboxNotice(
            id: item.id,
            title: item.capture.descriptionRaw,
            message: "Saved without its event (the event or transaction no longer exists)."
        ))
    }

    /// The retry policy for a failed request. Transport errors, 401 and cancellation stop the whole flush;
    /// a permanent rejection parks the item; anything else the server said leaves the item queued for a later flush.
    private static func outcome(for error: any Error) -> Outcome {
        guard let error = error as? APIError else { return .stop }
        switch error {
        case .server(let server) where server.status == HTTPStatus.unauthorized:
            return .stop
        case .server(let server) where isPermanentRejection(server.status):
            return .rejected(server.message)
        case .server(let server):
            return .skip(server.message)
        case .encoding:
            return .rejected(error.localizedDescription)
        case .decoding:
            return .skip("The server's reply couldn't be read.")
        case .transport:
            return .stop
        }
    }

    /// A 4xx the same request will get again, so retrying it is pointless.
    private static func isPermanentRejection(_ status: Int) -> Bool {
        (400..<500).contains(status) && status != HTTPStatus.unauthorized && status != HTTPStatus.tooManyRequests
    }

    /// Runs a store write; on failure logs it, reports the storage problem and returns false.
    private func persist(_ write: () throws -> Void) -> Bool {
        do {
            try write()
            return true
        } catch {
            noteWriteFailure(error)
            return false
        }
    }

    private func noteWriteFailure(_ error: any Error) {
        Self.logger.error("Outbox write failed: \(error.localizedDescription, privacy: .public)")
        writeProblem = "Unsent captures can't be updated on this device right now."
    }

    private func noteUnreadable(_ error: any Error) {
        Self.logger.error("Outbox listing failed: \(error.localizedDescription, privacy: .public)")
        readProblem = "Unsent captures can't be read right now."
    }

    /// Republishes the stored items. When the listing fails the last known queue stays on screen.
    private func reload() {
        guard let store else { return }
        do {
            let contents = try store.allItems()
            pending = contents.items.filter { $0.failure == nil }
            failed = contents.items.filter { $0.failure != nil }
            unreadable = contents.unreadable
            readProblem = nil
        } catch {
            noteUnreadable(error)
        }
    }
}
