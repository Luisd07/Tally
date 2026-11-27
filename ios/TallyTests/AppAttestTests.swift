import CryptoKit
import DeviceCheck
import Foundation
import os
import Testing
@testable import Tally

/// Stands in for `DCAppAttestService`, recording the client-data hashes it was asked to sign.
final class FakeAttestationService: AttestationService {
    var isSupported = true
    let newKeyId = Data("new-key".utf8).base64EncodedString()
    let attestation = Data("attestation".utf8)
    let assertionBytes = Data("assertion".utf8)
    private(set) var generatedKeys = 0
    private(set) var attestHashes: [Data] = []
    private(set) var assertionHashes: [(keyId: String, hash: Data)] = []
    /// Thrown by the next `generateAssertion`, then cleared.
    var nextAssertionError: (any Error)?

    func generateKey() async throws -> String {
        generatedKeys += 1
        return newKeyId
    }

    func attestKey(_ keyId: String, clientDataHash: Data) async throws -> Data {
        attestHashes.append(clientDataHash)
        return attestation
    }

    func generateAssertion(_ keyId: String, clientDataHash: Data) async throws -> Data {
        if let error = nextAssertionError {
            nextAssertionError = nil
            throw error
        }
        assertionHashes.append((keyId, clientDataHash))
        return assertionBytes
    }
}

final class MemoryAttestKeyStore: AttestKeyStore {
    var keys: [URL: String] = [:]
    func keyId(for server: URL) -> String? { keys[server] }
    func save(_ keyId: String, for server: URL) { keys[server] = keyId }
    func delete(for server: URL) { keys[server] = nil }
}

// MARK: - Network (shares StubURLProtocol, so these run in the serialized suite)

extension APIClientTests {
    private var attestServer: URL { URL(string: Session.productionServerURL)! }

    /// Challenge `n` is 32 bytes of `n`, with a fixed id per `n`.
    private nonisolated static func challenge(_ n: Int) -> (id: UUID, bytes: Data) {
        (UUID(uuidString: String(format: "00000000-0000-4000-8000-%012d", n))!, Data(repeating: UInt8(n), count: 32))
    }

    private nonisolated static let tokens = Data(#"{"access_token":"a1","refresh_token":"r1","token_type":"bearer","expires_in":900}"#.utf8)
    private nonisolated static let unauthorized = Data(#"{"error":{"code":"unauthorized","message":"Not signed in.","request_id":"r1"}}"#.utf8)

    /// A server for the attest, sign-in and refresh routes. Challenges count up from 1. `refuseAttested`
    /// answers 401 to a sign-in or refresh carrying assertion headers; `refuseAttest` refuses `POST /attest`.
    private func attestingServer(refuseAttested: Bool = false, refuseAttest: Bool = false) -> AsyncStream<URLRequest> {
        let (requests, seen) = AsyncStream.makeStream(of: URLRequest.self)
        let issued = OSAllocatedUnfairLock(initialState: 0)
        StubURLProtocol.respond { request in
            seen.yield(request)
            let path = request.url?.path() ?? ""
            switch path {
            case "/api/auth/attest/challenge":
                let n = issued.withLock { count -> Int in count += 1; return count }
                let challenge = Self.challenge(n)
                let body = #"{"challenge_id":"\#(challenge.id.uuidString.lowercased())","challenge":"\#(challenge.bytes.base64EncodedString())"}"#
                return (StubURLProtocol.response(to: request, status: 200), Data(body.utf8))
            case "/api/auth/attest":
                if refuseAttest { return (StubURLProtocol.response(to: request, status: 401), Self.unauthorized) }
                return (StubURLProtocol.response(to: request, status: 201), Data(#"{"key_id":"x","created_at":"2026-10-24T00:00:00Z"}"#.utf8))
            default:
                if refuseAttested, request.value(forHTTPHeaderField: AppAttest.keyIdHeader) != nil {
                    return (StubURLProtocol.response(to: request, status: 401), Self.unauthorized)
                }
                return (StubURLProtocol.response(to: request, status: 200), Self.tokens)
            }
        }
        return requests
    }

    private func attestedAPI(_ service: FakeAttestationService, _ store: MemoryAttestKeyStore) -> AuthAPI {
        AuthAPI(
            serverURL: attestServer, urlSession: StubURLProtocol.makeSession(),
            attestation: AppAttest(service: service, store: store)
        )
    }

    private func sha256(_ data: Data) -> Data { Data(SHA256.hash(data: data)) }

    @Test func attestsOnceThenSignsEachSignInAndRefresh() async throws {
        let requests = attestingServer()
        let service = FakeAttestationService()
        let store = MemoryAttestKeyStore()
        let api = attestedAPI(service, store)

        _ = try await api.signIn(challengeId: UUID(), credential: ["id": "c1"])
        _ = try await api.refresh("r0")

        var iterator = requests.makeAsyncIterator()
        var seen: [URLRequest] = []
        for _ in 0..<6 { seen.append(try #require(await iterator.next())) }
        #expect(seen.map { $0.url?.path() ?? "" } == [
            "/api/auth/attest/challenge", "/api/auth/attest",
            "/api/auth/attest/challenge", "/api/auth/signin/verify",
            "/api/auth/attest/challenge", "/api/auth/refresh",
        ])

        // Attestation: the key, Apple's object and the first challenge; the hash is of the challenge alone.
        let attestBody = try #require(bodyData(seen[1]))
        let attest = try #require(JSONSerialization.jsonObject(with: attestBody) as? [String: String])
        #expect(attest["key_id"] == service.newKeyId)
        #expect(attest["attestation_object"] == service.attestation.base64EncodedString())
        #expect(attest["challenge_id"].flatMap(UUID.init(uuidString:)) == Self.challenge(1).id)
        #expect(service.attestHashes == [sha256(Self.challenge(1).bytes)])
        #expect(store.keys[attestServer] == service.newKeyId)

        // Each assertion signs the exact body bytes sent, followed by that request's own challenge.
        let signInBody = try #require(bodyData(seen[3]))
        let refreshBody = try #require(bodyData(seen[5]))
        #expect(service.assertionHashes.map(\.hash) == [
            sha256(signInBody + Self.challenge(2).bytes), sha256(refreshBody + Self.challenge(3).bytes),
        ])
        #expect(seen[3].value(forHTTPHeaderField: AppAttest.keyIdHeader) == service.newKeyId)
        #expect(seen[3].value(forHTTPHeaderField: AppAttest.assertionHeader) == service.assertionBytes.base64EncodedString())
        #expect(seen[3].value(forHTTPHeaderField: AppAttest.challengeHeader) == Self.challenge(2).id.uuidString.lowercased())
        #expect(seen[5].value(forHTTPHeaderField: AppAttest.challengeHeader) == Self.challenge(3).id.uuidString.lowercased())
        #expect(service.generatedKeys == 1)
    }

    @Test func withoutAppAttestSignInGoesPlain() async throws {
        let requests = attestingServer()
        let service = FakeAttestationService()
        service.isSupported = false
        let api = attestedAPI(service, MemoryAttestKeyStore())

        _ = try await api.signIn(challengeId: UUID(), credential: ["id": "c1"])

        var iterator = requests.makeAsyncIterator()
        let request = try #require(await iterator.next())
        #expect(request.url?.path() == "/api/auth/signin/verify")
        #expect(request.value(forHTTPHeaderField: AppAttest.keyIdHeader) == nil)
        #expect(service.generatedKeys == 0)
    }

    @Test func refusedAssertionDropsTheKeyAndRetriesPlain() async throws {
        let requests = attestingServer(refuseAttested: true)
        let service = FakeAttestationService()
        let store = MemoryAttestKeyStore()
        store.keys[attestServer] = "stale-key"
        let api = attestedAPI(service, store)

        let pair = try await api.refresh("r0")
        #expect(pair == TokenPair(accessToken: "a1", refreshToken: "r1"))

        var iterator = requests.makeAsyncIterator()
        _ = try #require(await iterator.next())
        let attested = try #require(await iterator.next())
        let plain = try #require(await iterator.next())
        #expect(attested.value(forHTTPHeaderField: AppAttest.keyIdHeader) == "stale-key")
        #expect(plain.url?.path() == "/api/auth/refresh")
        #expect(plain.value(forHTTPHeaderField: AppAttest.keyIdHeader) == nil)
        #expect(bodyData(plain) == bodyData(attested))
        #expect(store.keys[attestServer] == nil)
    }

    @Test func lostKeyIsAttestedAgain() async throws {
        let requests = attestingServer()
        let service = FakeAttestationService()
        service.nextAssertionError = DCError(.invalidKey)
        let store = MemoryAttestKeyStore()
        store.keys[attestServer] = "lost-key"
        let api = attestedAPI(service, store)

        _ = try await api.signIn(challengeId: UUID(), credential: ["id": "c1"])

        var iterator = requests.makeAsyncIterator()
        var paths: [String] = []
        for _ in 0..<5 { paths.append(try #require(await iterator.next()).url?.path() ?? "") }
        #expect(paths == [
            "/api/auth/attest/challenge",
            "/api/auth/attest/challenge", "/api/auth/attest",
            "/api/auth/attest/challenge", "/api/auth/signin/verify",
        ])
        #expect(store.keys[attestServer] == service.newKeyId)
        #expect(service.assertionHashes.map(\.keyId) == [service.newKeyId])
    }

    @Test func refusedAttestationSendsSignInPlainAndKeepsNoKey() async throws {
        let requests = attestingServer(refuseAttest: true)
        let service = FakeAttestationService()
        let store = MemoryAttestKeyStore()
        let api = attestedAPI(service, store)

        _ = try await api.signIn(challengeId: UUID(), credential: ["id": "c1"])

        var iterator = requests.makeAsyncIterator()
        var seen: [URLRequest] = []
        for _ in 0..<3 { seen.append(try #require(await iterator.next())) }
        #expect(seen.map { $0.url?.path() ?? "" } == ["/api/auth/attest/challenge", "/api/auth/attest", "/api/auth/signin/verify"])
        #expect(seen[2].value(forHTTPHeaderField: AppAttest.keyIdHeader) == nil)
        #expect(store.keys.isEmpty)
    }
}
