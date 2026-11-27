import Foundation
import os
import Testing
@testable import Tally

extension APIClientTests {
    private static let runJSON = #"""
    {"id":"6a1d6c2e-8b1f-4c3e-9d2a-0f1e2d3c4b5a","started_at":"2026-10-24T02:00:00Z","finished_at":"2026-10-24T02:00:09Z",
     "status":"partial","trigger":"manual","new_count":41,"updated_count":2,
     "errors":[{"code":"gen","msg":"Chase: couldn't connect","account_id":"ACT-1"}]}
    """#

    @Test func connectClaimsTokenThenRunsFirstSync() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t", serverURL: server)
        let api = makeClient(session)
        let log = OSAllocatedUnfairLock(initialState: [(String, Data?)]())
        let runBody = Data(Self.runJSON.utf8)
        StubURLProtocol.respond { request in
            let path = request.url!.path()
            log.withLock { $0.append((path, requestBody(request))) }
            let body = path.hasSuffix("/claim")
                ? Data(#"{"claimed_at":"2026-10-24T02:00:00Z","host":"bridge.simplefin.org"}"#.utf8)
                : runBody
            return (StubURLProtocol.response(to: request, status: path.hasSuffix("/claim") ? 201 : 200), body)
        }
        let model = BankConnectionModel()

        let connected = await model.connect(setupToken: "  aHR0cHM6Ly9icmlkZ2U=\n", api: api) // gitleaks:allow (base64 of "https://bridge")
        #expect(connected)
        #expect(model.connectedHost == "bridge.simplefin.org")
        let calls = log.withLock { $0 }
        #expect(calls.map(\.0) == ["/api/sync/simplefin/claim", "/api/sync/simplefin/run"])
        let claim = try JSONSerialization.jsonObject(with: try #require(calls.first?.1)) as? [String: String]
        #expect(claim == ["setup_token": "aHR0cHM6Ly9icmlkZ2U="]) // gitleaks:allow
        let run = try #require(model.runs.first)
        #expect(run.status == .partial)
        #expect(run.newCount == 41)
        #expect(run.errors.first?.msg == "Chase: couldn't connect")
        #expect(model.message == nil)
    }

    @Test func usedSetupTokenShowsTheServerMessage() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t", serverURL: server)
        let api = makeClient(session)
        _ = reply(409, body: Data(#"{"error":{"code":"conflict","message":"This setup token was already used.","request_id":"r1"}}"#.utf8))
        let model = BankConnectionModel()

        let connected = await model.connect(setupToken: "used", api: api)
        #expect(!connected)
        #expect(model.message == "This setup token was already used.")
        #expect(model.runs.isEmpty)
    }
}

/// The body a stubbed request carried; `URLProtocol` sees it as a stream, not `httpBody`.
private nonisolated func requestBody(_ request: URLRequest) -> Data? {
    if let body = request.httpBody { return body }
    guard let stream = request.httpBodyStream else { return nil }
    stream.open()
    defer { stream.close() }
    var data = Data()
    var buffer = [UInt8](repeating: 0, count: 1024)
    while stream.hasBytesAvailable {
        let count = stream.read(&buffer, maxLength: buffer.count)
        if count <= 0 { break }
        data.append(buffer, count: count)
    }
    return data
}
