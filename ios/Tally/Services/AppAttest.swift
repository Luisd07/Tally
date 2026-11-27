import CryptoKit
import DeviceCheck
import Foundation
import os

/// The App Attest key calls (`DCAppAttestService`). `DeviceAttestationService` is the real one; tests stand in their own.
protocol AttestationService {
    var isSupported: Bool { get }
    func generateKey() async throws -> String
    func attestKey(_ keyId: String, clientDataHash: Data) async throws -> Data
    func generateAssertion(_ keyId: String, clientDataHash: Data) async throws -> Data
}

struct DeviceAttestationService: AttestationService {
    var isSupported: Bool { DCAppAttestService.shared.isSupported }

    func generateKey() async throws -> String {
        try await DCAppAttestService.shared.generateKey()
    }

    func attestKey(_ keyId: String, clientDataHash: Data) async throws -> Data {
        try await DCAppAttestService.shared.attestKey(keyId, clientDataHash: clientDataHash)
    }

    func generateAssertion(_ keyId: String, clientDataHash: Data) async throws -> Data {
        try await DCAppAttestService.shared.generateAssertion(keyId, clientDataHash: clientDataHash)
    }
}

/// Where the attested key's id is kept: one per server, since each server holds its own attested keys.
protocol AttestKeyStore {
    func keyId(for server: URL) -> String?
    func save(_ keyId: String, for server: URL)
    func delete(for server: URL)
}

struct KeychainAttestKeyStore: AttestKeyStore {
    private func account(_ server: URL) -> String { "app_attest_key_id.\(server.host() ?? server.absoluteString)" }

    func keyId(for server: URL) -> String? { (try? Keychain.read(account(server))) ?? nil }
    func save(_ keyId: String, for server: URL) { try? Keychain.save(keyId, for: account(server)) }
    func delete(for server: URL) { try? Keychain.delete(account(server)) }
}

/// App Attest on sign-in and refresh (#75): proves those requests come from this app on a real iPhone.
///
/// The first time, it makes a device key and has the server check Apple's attestation of it
/// (`POST /api/auth/attest`, with clientDataHash = SHA-256 of the challenge). Each sign-in and refresh then
/// carries an assertion over SHA-256(request body + a fresh challenge) in three `X-App-Attest-*` headers.
///
/// Anything that goes wrong (no App Attest on the simulator, Apple unreachable, a refused attestation) leaves
/// the request without the headers, and the server decides: it lets them through while `APP_ATTEST_REQUIRED`
/// is off and refuses them once it's on.
final class AppAttest {
    static let shared = AppAttest()

    nonisolated static let keyIdHeader = "X-App-Attest-Key-Id"
    nonisolated static let assertionHeader = "X-App-Attest-Assertion"
    nonisolated static let challengeHeader = "X-App-Attest-Challenge"

    private static let logger = Logger(subsystem: "com.luisd07.tally", category: "AppAttest")

    private let service: any AttestationService
    private let store: any AttestKeyStore

    init(service: any AttestationService = DeviceAttestationService(), store: any AttestKeyStore = KeychainAttestKeyStore()) {
        self.service = service
        self.store = store
    }

    /// The assertion headers for a request carrying exactly `body`, or none when they can't be made.
    func headers(for body: Data, using api: AuthAPI) async -> [String: String] {
        guard service.isSupported else { return [:] }
        do {
            let keyId = try await attestedKeyId(using: api)
            do {
                return try await assertion(keyId: keyId, body: body, using: api)
            } catch let error as DCError where error.code == .invalidKey {
                // The key is gone (a reinstall, a restored phone): attest a new one.
                discardKey(for: api.serverURL)
                return try await assertion(keyId: try await attestedKeyId(using: api), body: body, using: api)
            }
        } catch is CancellationError {
            return [:]
        } catch {
            Self.logger.error("App Attest skipped: \(error.localizedDescription, privacy: .public)")
            return [:]
        }
    }

    /// Forgets the key for `server`, so the next request attests a new one.
    func discardKey(for server: URL) {
        store.delete(for: server)
    }

    /// The stored key, or a new one the server has accepted. A key is stored only after the server accepts it.
    private func attestedKeyId(using api: AuthAPI) async throws -> String {
        if let keyId = store.keyId(for: api.serverURL) { return keyId }
        let keyId = try await service.generateKey()
        let challenge = try await api.attestChallenge()
        let attestation = try await service.attestKey(keyId, clientDataHash: Data(SHA256.hash(data: challenge.challenge)))
        try await api.attest(challengeId: challenge.challengeId, keyId: keyId, attestationObject: attestation)
        store.save(keyId, for: api.serverURL)
        return keyId
    }

    private func assertion(keyId: String, body: Data, using api: AuthAPI) async throws -> [String: String] {
        let challenge = try await api.attestChallenge()
        let assertion = try await service.generateAssertion(
            keyId, clientDataHash: Data(SHA256.hash(data: body + challenge.challenge))
        )
        return [
            Self.keyIdHeader: keyId,
            Self.assertionHeader: assertion.base64EncodedString(),
            Self.challengeHeader: challenge.challengeId.uuidString.lowercased(),
        ]
    }
}
