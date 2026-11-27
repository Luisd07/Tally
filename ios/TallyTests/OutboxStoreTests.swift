import Foundation
import Testing
@testable import Tally

@MainActor
struct OutboxStoreTests {
    let directory = FileManager.default.temporaryDirectory.appending(path: "outbox-test-\(UUID().uuidString)")

    private func capture(_ description: String = "HERTZ") -> Capture {
        Capture(
            accountId: UUID(),
            postedDate: CalendarDate(year: 2026, month: 10, day: 8),
            amount: Money(Decimal(string: "-85")!),
            descriptionRaw: description
        )
    }

    /// A store on this test's temporary directory, removed afterwards.
    private func withStore(_ body: (OutboxStore) throws -> Void) throws {
        defer { try? FileManager.default.removeItem(at: directory) }
        try body(try OutboxStore(directory: directory))
    }

    private func jpegURL(_ id: UUID) -> URL { directory.appending(path: "\(id.uuidString.lowercased()).jpg") }
    private func jsonURL(_ id: UUID) -> URL { directory.appending(path: "\(id.uuidString.lowercased()).json") }

    @Test func roundTripsWithoutReceipt() throws {
        defer { try? FileManager.default.removeItem(at: directory) }
        let outbox = try OutboxStore(directory: directory)
        let item = try outbox.enqueue(capture())

        let pending = try OutboxStore(directory: directory).allItems().items
        #expect(pending.map(\.capture) == [item.capture])
        #expect(pending.first?.capture.receiptId == nil)
        #expect(!FileManager.default.fileExists(atPath: jpegURL(item.id).path()))
    }

    @Test func roundTripsWithReceipt() throws {
        defer { try? FileManager.default.removeItem(at: directory) }
        let outbox = try OutboxStore(directory: directory)
        let jpeg = Data([0xFF, 0xD8, 0xFF, 0xD9])
        let item = try outbox.enqueue(capture(), receiptJPEG: jpeg)

        let fresh = try OutboxStore(directory: directory)
        #expect(try fresh.allItems().items.first?.capture.receiptId == item.capture.receiptId)
        #expect(item.capture.receiptId != nil)
        #expect(try fresh.receiptJPEG(for: item.id) == jpeg)
        let json = try String(contentsOf: jsonURL(item.id), encoding: .utf8)
        #expect(!json.contains("0xFF"))
    }

    @Test func preservesEnqueueOrderAcrossInstances() throws {
        defer { try? FileManager.default.removeItem(at: directory) }
        let outbox = try OutboxStore(directory: directory)
        let ids = try (0..<5).map { try outbox.enqueue(capture("item \($0)")).id }

        #expect(try OutboxStore(directory: directory).allItems().items.map(\.id) == ids)
    }

    @Test func removeDeletesBothFiles() throws {
        defer { try? FileManager.default.removeItem(at: directory) }
        let outbox = try OutboxStore(directory: directory)
        let item = try outbox.enqueue(capture(), receiptJPEG: Data([1, 2, 3]))
        #expect(FileManager.default.fileExists(atPath: jsonURL(item.id).path()))
        #expect(FileManager.default.fileExists(atPath: jpegURL(item.id).path()))

        try outbox.remove(item.id)
        #expect(!FileManager.default.fileExists(atPath: jsonURL(item.id).path()))
        #expect(!FileManager.default.fileExists(atPath: jpegURL(item.id).path()))
        #expect(try outbox.allItems().items.isEmpty)
    }

    @Test func markFailedPersists() throws {
        defer { try? FileManager.default.removeItem(at: directory) }
        let outbox = try OutboxStore(directory: directory)
        let item = try outbox.enqueue(capture())
        try outbox.markFailed(item.id, message: "Unknown account")

        let fresh = try OutboxStore(directory: directory).allItems().items
        #expect(fresh.count == 1)
        #expect(fresh.first?.failure == "Unknown account")
        #expect(fresh.first?.capture == item.capture)
    }

    @Test func filesAreCompleteProtectedAndDirectoryExcludedFromBackup() throws {
        defer { try? FileManager.default.removeItem(at: directory) }
        let outbox = try OutboxStore(directory: directory)
        let item = try outbox.enqueue(capture(), receiptJPEG: Data([1, 2, 3]))

        #expect(OutboxStore.writeOptions.contains(.completeFileProtection))
        for url in [jsonURL(item.id), jpegURL(item.id)] {
            let attributes = try FileManager.default.attributesOfItem(atPath: url.path())
            let protection = attributes[.protectionKey] as? FileProtectionType
            // The simulator may not report a protection class; the write options above are the guarantee then.
            if let protection { #expect(protection == .complete) }
        }
        let values = try directory.resourceValues(forKeys: [.isExcludedFromBackupKey])
        #expect(values.isExcludedFromBackup == true)
    }

    @Test func reportsAFileThatReadsButDoesNotDecodeAsUnreadable() throws {
        try withStore { store in
            let good = try store.enqueue(capture())
            let bad = UUID()
            try Data("garbage".utf8).write(to: jsonURL(bad))

            let contents = try store.allItems()
            #expect(contents.items.map(\.id) == [good.id])
            #expect(contents.unreadable == [bad])

            try store.remove(bad)
            #expect(try store.allItems().unreadable.isEmpty)
        }
    }

    @Test func throwsWhenAFileCannotBeReadInsteadOfReportingAnEmptyQueue() throws {
        try withStore { store in
            let item = try store.enqueue(capture())
            try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: jsonURL(item.id).path())
            defer { try? FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: jsonURL(item.id).path()) }

            #expect(throws: (any Error).self) { try store.allItems() }
        }
    }

    @Test func loadsAFileWrittenBeforeTheNewerFieldsExisted() throws {
        try withStore { store in
            let item = try store.enqueue(capture())
            var json = try #require(JSONSerialization.jsonObject(with: Data(contentsOf: jsonURL(item.id))) as? [String: Any])
            for key in ["rowSent", "failure", "lastError", "attempts"] { json[key] = nil }
            try JSONSerialization.data(withJSONObject: json).write(to: jsonURL(item.id))

            let loaded = try #require(try store.allItems().items.first)
            #expect(loaded.capture == item.capture)
            #expect(!loaded.rowSent)
            #expect(loaded.attempts == 0)
            #expect(loaded.lastError == nil)
        }
    }

    @Test func recordAttemptPersistsTheErrorAndCount() throws {
        try withStore { store in
            let item = try store.enqueue(capture())
            try store.recordAttempt(item.id, error: "Try again.")
            try store.recordAttempt(item.id, error: "Try again later.")

            let loaded = try #require(try OutboxStore(directory: directory).allItems().items.first)
            #expect(loaded.lastError == "Try again later.")
            #expect(loaded.attempts == 2)
        }
    }

    @Test func removesReceiptImagesWithoutACaptureFileWhenOpened() throws {
        try withStore { store in
            let kept = try store.enqueue(capture(), receiptJPEG: Data([1, 2, 3]))
            let orphan = UUID()
            try Data([9]).write(to: jpegURL(orphan))

            _ = try OutboxStore(directory: directory)
            #expect(!FileManager.default.fileExists(atPath: jpegURL(orphan).path()))
            #expect(FileManager.default.fileExists(atPath: jpegURL(kept.id).path()))
        }
    }

    @Test func removesTheReceiptImageWhenTheCaptureFileCannotBeWritten() throws {
        try withStore { store in
            let blocked = capture()
            try FileManager.default.createDirectory(at: jsonURL(blocked.id), withIntermediateDirectories: false)

            #expect(throws: (any Error).self) { try store.enqueue(blocked, receiptJPEG: Data([1, 2, 3])) }
            #expect(!FileManager.default.fileExists(atPath: jpegURL(blocked.id).path()))
        }
    }

    @Test func reEnqueuingWithoutAReceiptRemovesTheOldImage() throws {
        try withStore { store in
            let first = try store.enqueue(capture(), receiptJPEG: Data([1, 2, 3]))
            let again = try store.enqueue(first.capture)

            #expect(again.capture.receiptId == nil)
            #expect(!FileManager.default.fileExists(atPath: jpegURL(first.id).path()))
        }
    }
}
