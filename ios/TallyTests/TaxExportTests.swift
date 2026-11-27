import Foundation
import Testing
@testable import Tally

extension APIClientTests {
    private func zipFixture() throws -> Data { try bundleFixture("tally-tax-2026", ext: "zip") }

    @Test func taxExportSendsTheYear() async throws {
        defer { try? fixture.cleanUp(); ExportFile.sweep() }
        let api = makeClient(fixture.make())
        let requests = reply(200, body: try zipFixture())
        var iterator = requests.makeAsyncIterator()

        _ = try await api.taxExport(year: 2026)
        let request = try #require(await iterator.next())
        #expect(request.httpMethod == "GET")
        #expect(request.url?.path == "/api/tax/export")
        #expect(request.url?.query == "year=2026")
    }

    @Test func taxExportWritesTheProtectedZip() async throws {
        defer { try? fixture.cleanUp(); ExportFile.sweep() }
        let api = makeClient(fixture.make())
        let zip = try bundleFixture("tally-tax-2026", ext: "zip")
        _ = reply(200, body: zip)

        let url = try ExportFile.write(try await api.taxExport(year: 2026), named: ExportFile.taxZipName(year: 2026), in: ExportFile.directory(for: .taxZip))
        #expect(url.lastPathComponent == ExportFile.taxZipName(year: 2026))
        #expect(url.deletingLastPathComponent().standardizedFileURL == ExportFile.directory(for: .taxZip).standardizedFileURL)
        #expect(try Data(contentsOf: url) == zip)
        #expect(ExportFile.writeOptions.contains(.completeFileProtection))
        // The simulator may not report a protection class; the write options above are the guarantee then.
        let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
        if let protection = attributes[.protectionKey] as? FileProtectionType { #expect(protection == .complete) }
    }

    @Test func taxExportMapsAnErrorEnvelope() async throws {
        defer { try? fixture.cleanUp(); ExportFile.sweep() }
        let api = makeClient(fixture.make())
        _ = reply(404, body: Data(#"{"error":{"code":"not_found","message":"No such year.","request_id":"r1"}}"#.utf8))

        let error = await #expect(throws: APIError.self) { try await api.taxExport(year: 2026) }
        #expect(error?.status == 404)
        #expect(error?.errorDescription == "No such year.")
        #expect(!FileManager.default.fileExists(atPath: ExportFile.directory(for: .taxZip).path))
    }

    @Test func taxExportUnauthorizedSignsOut() async throws {
        defer { try? fixture.cleanUp(); ExportFile.sweep() }
        let session = fixture.make()
        try session.signIn(token: "expired", serverURL: server)
        let api = makeClient(session)
        _ = reply(401)

        let error = await #expect(throws: APIError.self) { try await api.taxExport(year: 2026) }
        #expect(error?.isUnauthorized == true)
        #expect(!session.isSignedIn)
    }

    @Test func removeLeftoversDeletesTheFile() async throws {
        defer { try? fixture.cleanUp(); ExportFile.sweep() }
        let api = makeClient(fixture.make())
        _ = reply(200, body: try zipFixture())
        let url = try ExportFile.write(try await api.taxExport(year: 2026), named: ExportFile.taxZipName(year: 2026), in: ExportFile.directory(for: .taxZip))
        #expect(FileManager.default.fileExists(atPath: url.path))

        ExportFile.sweep()
        #expect(!FileManager.default.fileExists(atPath: url.path))
    }
}
