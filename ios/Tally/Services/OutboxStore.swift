import Foundation
import os

/// One unsent capture and what its send attempts said.
struct OutboxItem: Codable, Hashable, Identifiable {
    let capture: Capture
    let enqueuedAt: Date
    /// Why the server will never accept it; set means parked until retry or discard.
    var failure: String?
    /// The server has the row; only the receipt (if any) is still to send.
    var rowSent = false
    /// True once the event PATCH succeeded or was abandoned because the server said 404; stays false for a
    /// capture with no event. Only the receipt (if any) is still to send.
    var eventLinked = false
    /// What the last attempt that got an answer but didn't finish said; the item is still retried.
    var lastError: String?
    var attempts = 0

    var id: UUID { capture.id }

    init(capture: Capture, enqueuedAt: Date) {
        self.capture = capture
        self.enqueuedAt = enqueuedAt
    }

    /// Every field added after the first release must be read with `decodeIfPresent`, so files already
    /// on the phone keep loading.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        capture = try container.decode(Capture.self, forKey: .capture)
        enqueuedAt = try container.decode(Date.self, forKey: .enqueuedAt)
        failure = try container.decodeIfPresent(String.self, forKey: .failure)
        rowSent = try container.decodeIfPresent(Bool.self, forKey: .rowSent) ?? false
        eventLinked = try container.decodeIfPresent(Bool.self, forKey: .eventLinked) ?? false
        lastError = try container.decodeIfPresent(String.self, forKey: .lastError)
        attempts = try container.decodeIfPresent(Int.self, forKey: .attempts) ?? 0
    }
}

/// What is on disk: the items that decode, and the ids of `.json` files that were read but don't.
struct OutboxContents {
    let items: [OutboxItem]
    let unreadable: [UUID]
}

/// Unsent captures, stored as JSON plus receipt images in Application Support
/// with complete file protection. Each item is deleted once the server has it.
final class OutboxStore {
    let directory: URL
    private let fileManager = FileManager.default

    private static let logger = Logger(subsystem: "com.luisd07.tally", category: "OutboxStore")

    /// Defaults to `Application Support/Outbox`; the directory is created if missing and excluded from backup.
    /// Receipt images left without a capture file (a crash between the two writes) are deleted.
    init(directory: URL? = nil) throws {
        let directory = try directory ?? fileManager
            .url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
            .appending(path: "Outbox", directoryHint: .isDirectory)
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        var excluded = directory
        try excluded.setResourceValues(values)
        self.directory = directory
        removeOrphanImages()
    }

    /// Every stored item, oldest first. Throws when the directory or a file can't be read (for example
    /// protected data is unavailable), which is not the same as an empty outbox; a file that reads but
    /// doesn't decode is reported in `unreadable` instead.
    func allItems() throws -> OutboxContents {
        var items: [OutboxItem] = []
        var unreadable: [UUID] = []
        for url in try fileManager.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
        where url.pathExtension == "json" {
            let data = try Data(contentsOf: url)
            do {
                items.append(try Self.decoder.decode(OutboxItem.self, from: data))
            } catch {
                let name = url.lastPathComponent
                Self.logger.error("Outbox file \(name, privacy: .public) doesn't decode")
                if let id = UUID(uuidString: url.deletingPathExtension().lastPathComponent) {
                    unreadable.append(id)
                }
            }
        }
        items.sort { ($0.enqueuedAt, $0.id.uuidString) < ($1.enqueuedAt, $1.id.uuidString) }
        return OutboxContents(items: items, unreadable: unreadable.sorted { $0.uuidString < $1.uuidString })
    }

    /// Writes `<id>.json` and, when given, `<id>.jpg`. The image is written first so a stored capture never
    /// lacks its receipt; if the capture file then fails to write, the image is removed again.
    @discardableResult
    func enqueue(_ capture: Capture, receiptJPEG: Data? = nil) throws -> OutboxItem {
        var capture = capture
        capture.receiptId = receiptJPEG == nil ? nil : capture.receiptId ?? UUID()
        let image = imageURL(capture.id)
        if let receiptJPEG {
            try receiptJPEG.write(to: image, options: Self.writeOptions)
        } else {
            try removeIfPresent(image)
        }
        let item = OutboxItem(capture: capture, enqueuedAt: Date())
        do {
            try write(item)
        } catch {
            if receiptJPEG != nil { try? removeIfPresent(image) }
            throw error
        }
        return item
    }

    /// The queued receipt bytes for `id`: nil when there is no image file,
    /// throws when the file exists but can't be read.
    func receiptJPEG(for id: UUID) throws -> Data? {
        let url = imageURL(id)
        guard fileManager.fileExists(atPath: url.path()) else { return nil }
        return try Data(contentsOf: url)
    }

    /// Deletes both files for `id`.
    func remove(_ id: UUID) throws {
        try removeIfPresent(itemURL(id))
        try removeIfPresent(imageURL(id))
    }

    /// Records why the server will never accept the item; the capture stays queued, parked.
    func markFailed(_ id: UUID, message: String) throws {
        try update(id) { $0.failure = message }
    }

    /// Records an attempt that got an answer but didn't finish; the item is sent again on a later flush.
    func recordAttempt(_ id: UUID, error message: String) throws {
        try update(id) {
            $0.lastError = message
            $0.attempts += 1
        }
    }

    /// Clears a recorded failure so the item is sent again.
    func clearFailure(_ id: UUID) throws {
        try update(id) { $0.failure = nil }
    }

    /// Records that the server has the row, so a later send only retries the receipt.
    func markRowSent(_ id: UUID) throws {
        try update(id) { $0.rowSent = true }
    }

    /// Records that the capture's event is linked, so a later send skips the PATCH.
    func markEventLinked(_ id: UUID) throws {
        try update(id) { $0.eventLinked = true }
    }

    static let writeOptions: Data.WritingOptions = [.atomic, .completeFileProtection]

    private static let decoder = JSONDecoder()
    private static let encoder = JSONEncoder()

    private func update(_ id: UUID, _ change: (inout OutboxItem) -> Void) throws {
        var item = try Self.decoder.decode(OutboxItem.self, from: Data(contentsOf: itemURL(id)))
        change(&item)
        try write(item)
    }

    private func write(_ item: OutboxItem) throws {
        try Self.encoder.encode(item).write(to: itemURL(item.id), options: Self.writeOptions)
    }

    private func removeIfPresent(_ url: URL) throws {
        guard fileManager.fileExists(atPath: url.path()) else { return }
        try fileManager.removeItem(at: url)
    }

    private func removeOrphanImages() {
        let files = (try? fileManager.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? []
        for image in files where image.pathExtension == "jpg" {
            let capture = image.deletingPathExtension().appendingPathExtension("json")
            guard !fileManager.fileExists(atPath: capture.path()) else { continue }
            do {
                try fileManager.removeItem(at: image)
            } catch {
                let reason = error.localizedDescription
                Self.logger.error("Couldn't remove an orphan receipt image: \(reason, privacy: .public)")
            }
        }
    }

    private func itemURL(_ id: UUID) -> URL {
        directory.appending(path: "\(id.uuidString.lowercased()).json")
    }

    private func imageURL(_ id: UUID) -> URL {
        directory.appending(path: "\(id.uuidString.lowercased()).jpg")
    }
}
