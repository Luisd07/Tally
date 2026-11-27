import Foundation

/// A shared export's temp file (the tax zip, the transactions CSV). Financial data stays on the phone only while
/// the share sheet is up, so the file lives in its kind's directory with complete protection and is deleted when
/// the share completes, before each export and at launch.
nonisolated enum ExportFile {
    /// Each kind has its own directory, so one export never deletes another's file.
    enum Kind: String {
        case taxZip
        case transactionsCSV
    }

    static var root: URL {
        FileManager.default.temporaryDirectory.appending(path: "Export", directoryHint: .isDirectory)
    }

    static func directory(for kind: Kind, in root: URL = root) -> URL {
        root.appending(path: kind.rawValue, directoryHint: .isDirectory)
    }

    static func taxZipName(year: Int) -> String { "tally-tax-\(year).zip" }

    /// As the server names the month's CSV.
    @MainActor static func transactionsCSVName(month: MonthRef) -> String { "tally-transactions-\(month.apiString).csv" }

    static let writeOptions: Data.WritingOptions = [.atomic, .completeFileProtection]

    /// The phone could not store the file (full disk, locked device).
    struct WriteError: LocalizedError {
        var errorDescription: String? { "Couldn't save the export on this phone." }
    }

    /// Writes `data` as `name` in `directory`, replacing any earlier export.
    static func write(_ data: Data, named name: String, in directory: URL) throws(WriteError) -> URL {
        removeLeftovers(in: directory)
        let url = directory.appending(path: name)
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            try data.write(to: url, options: writeOptions)
        } catch {
            removeLeftovers(in: directory)
            throw WriteError()
        }
        return url
    }

    /// Deletes `directory` and everything in it.
    static func removeLeftovers(in directory: URL) {
        try? FileManager.default.removeItem(at: directory)
    }

    /// The launch sweep: every kind's directory, and the tax export's pre-#73 `TaxExport` directory.
    static func sweep(root: URL = root, temporary: URL = FileManager.default.temporaryDirectory) {
        removeLeftovers(in: root)
        removeLeftovers(in: temporary.appending(path: "TaxExport", directoryHint: .isDirectory))
    }
}
