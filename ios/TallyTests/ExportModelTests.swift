import Foundation
import Testing
@testable import Tally

extension APIClientTests {
    private func zipFixture() throws -> Data { try bundleFixture("tally-tax-2026", ext: "zip") }

    @Test @MainActor func exportFileNamesArePinned() {
        #expect(ExportFile.taxZipName(year: 2026) == "tally-tax-2026.zip")
        #expect(ExportFile.transactionsCSVName(month: MonthRef(apiString: "2026-10")!) == "tally-transactions-2026-10.csv")
    }

    @Test func exportModelIsExportingWhileTheDownloadRuns() async throws {
        defer { try? fixture.cleanUp(); ExportFile.sweep() }
        let api = makeClient(fixture.make())
        let zip = try zipFixture()
        let (received, receivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        StubURLProtocol.respond { request in
            receivedSignal.yield()
            for await _ in release { break }
            return (StubURLProtocol.response(to: request, status: 200), zip)
        }
        let model = ExportModel(kind: .taxZip)
        #expect(model.state == .idle)

        let running = Task { await model.export(named: ExportFile.taxZipName(year: 2026)) { try await api.taxExport(year: 2026) } }
        for await _ in received { break }
        #expect(model.isExporting)
        releaseSignal.yield()
        await running.value
        #expect(model.readyURL != nil)
    }

    @Test func exportFileLivesUntilTheShareCompletes() async throws {
        defer { try? fixture.cleanUp(); ExportFile.sweep() }
        let api = makeClient(fixture.make())
        _ = reply(200, body: try zipFixture())
        let model = ExportModel(kind: .taxZip)
        await model.export(named: ExportFile.taxZipName(year: 2026)) { try await api.taxExport(year: 2026) }
        let url = try #require(model.readyURL)
        #expect(url.lastPathComponent == ExportFile.taxZipName(year: 2026))

        model.sheetDismissed()
        #expect(model.state == .idle)
        #expect(FileManager.default.fileExists(atPath: url.path))

        model.finish()
        #expect(!FileManager.default.fileExists(atPath: url.path))
    }

    @Test func exportModelFinishDeletesTheReadyFile() async throws {
        defer { try? fixture.cleanUp(); ExportFile.sweep() }
        let api = makeClient(fixture.make())
        _ = reply(200, body: try zipFixture())
        let model = ExportModel(kind: .taxZip)
        await model.export(named: ExportFile.taxZipName(year: 2026)) { try await api.taxExport(year: 2026) }
        let url = try #require(model.readyURL)
        #expect(FileManager.default.fileExists(atPath: url.path))

        model.finish()
        #expect(model.state == .idle)
        #expect(!FileManager.default.fileExists(atPath: url.path))
    }

    @Test func exportModelReportsALocalWriteFailure() async throws {
        defer { try? fixture.cleanUp() }
        let blocker = FileManager.default.temporaryDirectory.appending(path: "ExportBlocker-\(UUID().uuidString)")
        try Data().write(to: blocker)
        defer { try? FileManager.default.removeItem(at: blocker) }
        let api = makeClient(fixture.make())
        _ = reply(200, body: try zipFixture())
        let model = ExportModel(kind: .taxZip, root: blocker)

        await model.export(named: ExportFile.taxZipName(year: 2026)) { try await api.taxExport(year: 2026) }
        #expect(model.failureMessage == "Couldn't save the export on this phone.")
        model.clearFailure()
        #expect(model.state == .idle)
    }

    @Test func exportModelFailsWithTheServerMessage() async throws {
        defer { try? fixture.cleanUp(); ExportFile.sweep() }
        let api = makeClient(fixture.make())
        _ = reply(404, body: Data(#"{"error":{"code":"not_found","message":"No such year.","request_id":"r1"}}"#.utf8))
        let model = ExportModel(kind: .taxZip)

        await model.export(named: ExportFile.taxZipName(year: 2026)) { try await api.taxExport(year: 2026) }
        #expect(model.state == .failed("No such year."))
        model.finish()
        #expect(model.state == .idle)
    }

    @Test func exportsOfDifferentKindsDoNotDeleteEachOther() async throws {
        defer { try? fixture.cleanUp(); ExportFile.sweep() }
        let api = makeClient(fixture.make())
        let csv = try bundleFixture("tally-transactions-2026-10", ext: "csv")
        let zip = try zipFixture()
        let tax = ExportModel(kind: .taxZip)
        let transactions = ExportModel(kind: .transactionsCSV)

        _ = reply(200, body: zip)
        await tax.export(named: ExportFile.taxZipName(year: 2026)) { try await api.taxExport(year: 2026) }
        let taxURL = try #require(tax.readyURL)
        _ = reply(200, body: csv)
        await transactions.export(named: "a.csv") { try await api.transactionsCSV(TransactionQuery()) }
        let csvURL = try #require(transactions.readyURL)
        #expect(FileManager.default.fileExists(atPath: taxURL.path))

        transactions.finish()
        #expect(FileManager.default.fileExists(atPath: taxURL.path))

        await transactions.export(named: "a.csv") { try await api.transactionsCSV(TransactionQuery()) }
        _ = reply(200, body: zip)
        await tax.export(named: ExportFile.taxZipName(year: 2026)) { try await api.taxExport(year: 2026) }
        #expect(FileManager.default.fileExists(atPath: try #require(transactions.readyURL).path))
        #expect(csvURL == transactions.readyURL)

        tax.finish()
        #expect(FileManager.default.fileExists(atPath: csvURL.path))
    }

    @Test func sweepRemovesEveryKindAndTheLegacyTaxDirectory() throws {
        let temporary = FileManager.default.temporaryDirectory.appending(path: "Sweep-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: temporary) }
        let root = temporary.appending(path: "Export", directoryHint: .isDirectory)
        let legacy = temporary.appending(path: "TaxExport", directoryHint: .isDirectory)
        var files: [URL] = []
        for directory in [ExportFile.directory(for: .taxZip, in: root), ExportFile.directory(for: .transactionsCSV, in: root), legacy] {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let file = directory.appending(path: "x")
            try Data("x".utf8).write(to: file)
            files.append(file)
        }

        ExportFile.sweep(root: root, temporary: temporary)
        for file in files { #expect(!FileManager.default.fileExists(atPath: file.path)) }
    }
}
