import Foundation
import Observation

/// A bank export picked for import, held in memory only and never written to disk.
struct ImportFile: Equatable {
    enum Kind: Equatable {
        case csv, ofx

        /// The extensions sent to the OFX endpoint, which parses QFX as OFX.
        static let ofxExtensions = ["ofx", "qfx"]

        /// OFX or QFX by the file's extension; anything else is read as CSV.
        init(filename: String) {
            let pathExtension = (filename as NSString).pathExtension.lowercased()
            self = Self.ofxExtensions.contains(pathExtension) ? .ofx : .csv
        }
    }

    /// The largest file the server takes.
    static let maxBytes = 2_000_000

    let name: String
    let data: Data

    var kind: Kind { Kind(filename: name) }

    var isTooLarge: Bool { data.count > Self.maxBytes }

    /// Reads the file at `url`, a security-scoped URL from the file importer, into memory. It reads one byte past
    /// the server's limit at most, so an oversize file is refused without loading all of it.
    static func read(from url: URL) throws -> ImportFile {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        let data = try handle.read(upToCount: maxBytes + 1) ?? Data()
        return ImportFile(name: url.lastPathComponent, data: data)
    }
}

/// The Import screen's state: the account, the picked file, the account's CSV column mapping and the latest
/// result. Nothing is stored; the file lives in memory until it is imported or the screen closes.
@Observable
final class ImportModel {
    enum MappingLoad {
        case idle, loading, failed(APIError)
        /// The account's saved mapping; nil when it has none.
        case loaded(ImportMapping?)
    }

    private(set) var accountId: UUID?
    private(set) var file: ImportFile?
    private(set) var mappingLoad = MappingLoad.idle
    /// The mapping being edited; shown while `isEditingMapping`.
    var draft = ImportMappingDraft()
    private(set) var isEditingMapping = false
    private(set) var isSavingMapping = false
    /// The latest failed mapping save. Cleared when the next one starts or the account changes.
    private(set) var mappingSaveError: APIError?
    private(set) var isImporting = false
    /// Why the latest import didn't happen, or why the file couldn't be read.
    private(set) var importFailure: String?
    private(set) var result: ImportResult?
    /// The name of the file `result` is for; its bytes are dropped once imported.
    private(set) var resultFileName: String?
    /// The picked CSV's first records, read once when it is picked.
    private var sample: CSVSample?
    private(set) var isGuessingMapping = false
    /// Why the latest AI guess didn't fill the draft.
    private(set) var guessError: String?
    /// The header and data rows sent for an AI guess; the server sends at most 5 data rows on.
    static let guessRecords = 6

    /// The account's saved mapping, once loaded.
    var savedMapping: ImportMapping? {
        if case .loaded(let mapping) = mappingLoad { mapping } else { nil }
    }

    /// The columns the mapping editor offers: the header's names, or with no header, the first rows' indexes.
    var columns: [String] {
        guard let sample else { return [] }
        return draft.hasHeader ? sample.names : sample.indexes
    }

    /// A column picker's options: the file's columns, plus `selection` when the file doesn't have it, so a saved
    /// mapping still shows.
    func columnOptions(including selection: String) -> [String] {
        var options: [String] = []
        for column in columns + [selection] where !column.isEmpty && !options.contains(column) {
            options.append(column)
        }
        return options
    }

    /// The saved mapping's header columns the picked CSV doesn't have, matched as the server matches them. The
    /// server refuses the whole file for any of them.
    var missingColumns: [String] {
        guard let savedMapping, savedMapping.hasHeader, let sample else { return [] }
        return savedMapping.columns.filter { sample.position(of: $0, hasHeader: true) == nil }
    }

    /// The picked CSV's first data row read with the draft; nil without a CSV or a data row.
    var preview: ImportPreview? {
        sample.flatMap { ImportPreview(sample: $0, draft: draft) }
    }

    var isFileTooLarge: Bool { file?.isTooLarge ?? false }

    /// Whether Import can be tapped: an account and a file, and for a CSV, a saved mapping not being edited whose
    /// columns the file has.
    var canImport: Bool {
        guard accountId != nil, let file, !isImporting else { return false }
        return file.kind == .ofx || (savedMapping != nil && !isEditingMapping && missingColumns.isEmpty)
    }

    /// Picks `accountId` and loads its mapping, dropping the previous account's result. A cancelled load changes
    /// nothing; only the latest account's load lands.
    func select(accountId: UUID?, api: APIClient) async {
        self.accountId = accountId
        clearResult()
        importFailure = isFileTooLarge ? Self.tooLargeMessage : nil
        mappingSaveError = nil
        isEditingMapping = false
        draft = ImportMappingDraft()
        guard let accountId else {
            mappingLoad = .idle
            return
        }
        await loadMapping(accountId: accountId, api: api)
    }

    /// Loads the selected account's mapping again, after a failed load.
    func reloadMapping(api: APIClient) async {
        guard let accountId else { return }
        await loadMapping(accountId: accountId, api: api)
    }

    private func loadMapping(accountId: UUID, api: APIClient) async {
        mappingLoad = .loading
        do {
            let mapping = try await api.importMapping(accountId: accountId)
            guard isCurrent(accountId) else { return }
            mappingLoad = .loaded(mapping)
            draft = mapping.map(ImportMappingDraft.init) ?? ImportMappingDraft()
            isEditingMapping = mapping == nil
        } catch is CancellationError {
            return
        } catch {
            guard isCurrent(accountId) else { return }
            mappingLoad = .failed(APIError(wrapping: error))
        }
    }

    /// Holds `file` for import, dropping the previous result.
    func pick(_ file: ImportFile) {
        self.file = file
        clearResult()
        importFailure = isFileTooLarge ? Self.tooLargeMessage : nil
        sample = file.kind == .csv ? CSVSample(file.data) : nil
    }

    /// Reads the file the importer picked; a failure to pick or read it is shown as `importFailure`.
    func pickFile(_ picked: Result<URL, any Error>) {
        do {
            pick(try ImportFile.read(from: picked.get()))
        } catch {
            importFailure = "Couldn't read the file: \(error.localizedDescription)"
        }
    }

    /// Asks the server to guess the columns from the file's first records (#172) and puts the guess in the draft
    /// for review. Saves nothing.
    func guessMapping(api: APIClient) async {
        guard let sample, let accountId, !isGuessingMapping else { return }
        isGuessingMapping = true
        guessError = nil
        defer { isGuessingMapping = false }
        do {
            let guess = try await api.guessImportMapping(records: Array(sample.records.prefix(Self.guessRecords)))
            guard isCurrent(accountId) else { return }
            draft = ImportMappingDraft(guess)
            isEditingMapping = true
        } catch is CancellationError {
            return
        } catch {
            if isCurrent(accountId) { guessError = error.localizedDescription }
        }
    }

    func editMapping() {
        mappingSaveError = nil
        isEditingMapping = true
    }

    /// Drops the edits and shows the saved mapping again; only when there is one.
    func cancelEditingMapping() {
        guard let savedMapping else { return }
        draft = ImportMappingDraft(savedMapping)
        mappingSaveError = nil
        isEditingMapping = false
    }

    /// Saves the draft as the account's mapping. On failure `mappingSaveError` says why (nil when cancelled) and
    /// the draft stays as it was. Returns whether it saved.
    @discardableResult
    func saveMapping(api: APIClient) async -> Bool {
        guard let accountId, case .success(let mapping) = draft.validated, !isSavingMapping else { return false }
        isSavingMapping = true
        mappingSaveError = nil
        defer { isSavingMapping = false }
        do {
            let saved = try await api.saveImportMapping(accountId: accountId, mapping)
            guard isCurrent(accountId) else { return false }
            mappingLoad = .loaded(saved)
            draft = ImportMappingDraft(saved)
            isEditingMapping = false
            return true
        } catch is CancellationError {
            return false
        } catch {
            if isCurrent(accountId) { mappingSaveError = APIError(wrapping: error) }
            return false
        }
    }

    /// Uploads the file to the selected account. A file over `ImportFile.maxBytes` is refused here without a
    /// request. Once imported, the file's bytes are dropped and only its name stays, for the result.
    func importFile(api: APIClient) async {
        guard canImport, let accountId, let file else { return }
        guard !file.isTooLarge else {
            importFailure = Self.tooLargeMessage
            return
        }
        isImporting = true
        importFailure = nil
        clearResult()
        defer { isImporting = false }
        do {
            let result = try await upload(file, accountId: accountId, api: api)
            guard isCurrent(accountId, file) else { return }
            finishImport(of: file, with: result)
        } catch is CancellationError {
            return
        } catch {
            guard isCurrent(accountId, file) else { return }
            importFailure = APIError(wrapping: error).localizedDescription
        }
    }

    private func upload(_ file: ImportFile, accountId: UUID, api: APIClient) async throws -> ImportResult {
        switch file.kind {
        case .csv: try await api.importCSV(accountId: accountId, data: file.data, filename: file.name)
        case .ofx: try await api.importOFX(accountId: accountId, data: file.data, filename: file.name)
        }
    }

    private func finishImport(of file: ImportFile, with result: ImportResult) {
        self.result = result
        resultFileName = file.name
        self.file = nil
        sample = nil
    }

    private func clearResult() {
        result = nil
        resultFileName = nil
    }

    /// Whether a response for `accountId` (and `file`, when given) is still for what the screen shows; a late one
    /// for an account or file since replaced is dropped.
    private func isCurrent(_ accountId: UUID, _ file: ImportFile? = nil) -> Bool {
        accountId == self.accountId && (file == nil || file == self.file)
    }

    private static let tooLargeMessage = "This file is over \(ImportFile.maxBytes / 1_000_000) MB, the largest the "
        + "server takes. Export a shorter date range and try again."
}
