import Foundation

/// The access and refresh tokens a passkey sign-in or a refresh returns.
nonisolated struct TokenPair: Decodable, Equatable, Sendable {
    let accessToken: String
    let refreshToken: String
}

/// What a new passkey needs, from `/api/auth/enroll/options`.
nonisolated struct PasskeyRegistrationOptions: Equatable, Sendable {
    let challengeId: UUID
    let relyingPartyId: String
    let challenge: Data
    let userId: Data
    let userName: String
}

/// A one-time App Attest challenge, from `GET /api/auth/attest/challenge`.
nonisolated struct AttestChallenge: Equatable, Sendable {
    let challengeId: UUID
    let challenge: Data
}

/// What signing in with a passkey needs, from `/api/auth/signin/options`.
nonisolated struct PasskeyAssertionOptions: Equatable, Sendable {
    let challengeId: UUID
    let relyingPartyId: String
    let challenge: Data
}

/// The calls under `/api/auth`. They carry no bearer token, and the WebAuthn credential has to reach the
/// server with its own camelCase keys (`clientDataJSON`), so they skip `APIClient` and its snake_case encoder.
/// Failures throw `APIError` (or `CancellationError`), the same as `APIClient`.
struct AuthAPI {
    let serverURL: URL
    var urlSession: URLSession = APIClient.defaultSession
    /// Adds App Attest assertions to sign-in and refresh; nil sends them without.
    var attestation: AppAttest? = nil

    func registrationOptions(code: String) async throws -> PasskeyRegistrationOptions {
        let reply: Ceremony<RegistrationJSON> = try await post("/api/auth/enroll/options", ["code": code])
        guard let challenge = Data(base64URLEncoded: reply.options.challenge),
              let userId = Data(base64URLEncoded: reply.options.user.id)
        else { throw Self.unreadable }
        return PasskeyRegistrationOptions(
            challengeId: reply.challengeId,
            relyingPartyId: reply.options.rp.id,
            challenge: challenge,
            userId: userId,
            userName: reply.options.user.name
        )
    }

    /// Stores the new passkey on the server. It doesn't sign in; `signIn` does that next.
    func register(challengeId: UUID, credential: [String: Any], deviceName: String?) async throws {
        var body: [String: Any] = ["challenge_id": challengeId.uuidString, "credential": credential]
        if let deviceName { body["device_name"] = deviceName }
        _ = try await send("/api/auth/enroll/verify", body)
    }

    func assertionOptions() async throws -> PasskeyAssertionOptions {
        let reply: Ceremony<AssertionJSON> = try await post("/api/auth/signin/options", [:])
        guard let challenge = Data(base64URLEncoded: reply.options.challenge) else { throw Self.unreadable }
        return PasskeyAssertionOptions(
            challengeId: reply.challengeId,
            relyingPartyId: reply.options.rpId,
            challenge: challenge
        )
    }

    func signIn(challengeId: UUID, credential: [String: Any]) async throws -> TokenPair {
        try decode(try await sendAttested(
            "/api/auth/signin/verify", ["challenge_id": challengeId.uuidString, "credential": credential]
        ))
    }

    /// Trades the refresh token for a new pair. The server rotates it: the old one stops working.
    func refresh(_ refreshToken: String) async throws -> TokenPair {
        try decode(try await sendAttested("/api/auth/refresh", ["refresh_token": refreshToken]))
    }

    // MARK: - App Attest

    func attestChallenge() async throws -> AttestChallenge {
        let reply: ChallengeJSON = try decode(try await send(path: "/api/auth/attest/challenge", method: "GET", body: nil))
        guard let challenge = Data(base64Encoded: reply.challenge) else { throw Self.unreadable }
        return AttestChallenge(challengeId: reply.challengeId, challenge: challenge)
    }

    /// Has the server check Apple's attestation of `keyId` and keep the key.
    func attest(challengeId: UUID, keyId: String, attestationObject: Data) async throws {
        _ = try await send("/api/auth/attest", [
            "challenge_id": challengeId.uuidString,
            "key_id": keyId,
            "attestation_object": attestationObject.base64EncodedString(),
        ])
    }

    /// Sends `body` with App Attest headers over its exact bytes. A 401 to a request that carried them drops
    /// the key and sends the request once more without, so a key the server no longer knows (or a fault in
    /// App Attest) can't lock the app out while the server doesn't require attestation. Once it does, the
    /// plain retry is refused too.
    private func sendAttested(_ path: String, _ body: [String: Any]) async throws -> Data {
        let data = try JSONSerialization.data(withJSONObject: body)
        guard let attestation else { return try await send(path: path, method: "POST", body: data) }
        let headers = await attestation.headers(for: data, using: self)
        if headers.isEmpty { return try await send(path: path, method: "POST", body: data) }
        do {
            return try await send(path: path, method: "POST", body: data, headers: headers)
        } catch let error as APIError where error.isUnauthorized {
            attestation.discardKey(for: serverURL)
            return try await send(path: path, method: "POST", body: data)
        }
    }

    /// Revokes the refresh token and every token rotated from it.
    func signOut(_ refreshToken: String) async throws {
        _ = try await send("/api/auth/signout", ["refresh_token": refreshToken])
    }

    // MARK: - Transport

    private static let unreadable = APIError.decoding(
        .dataCorrupted(.init(codingPath: [], debugDescription: "The server's challenge wasn't valid base64."))
    )

    private func post<T: Decodable>(_ path: String, _ body: [String: Any]) async throws -> T {
        try decode(try await send(path, body))
    }

    private func decode<T: Decodable>(_ data: Data) throws -> T {
        do {
            return try JSONDecoder.api.decode(T.self, from: data)
        } catch let error as DecodingError {
            throw APIError.decoding(error)
        }
    }

    private func send(_ path: String, _ body: [String: Any]) async throws -> Data {
        try await send(path: path, method: "POST", body: try JSONSerialization.data(withJSONObject: body))
    }

    /// The body of a 2xx response; anything else throws.
    private func send(path: String, method: String, body: Data?, headers: [String: String] = [:]) async throws -> Data {
        var request = URLRequest(url: serverURL.appending(path: path))
        request.httpMethod = method
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        for (name, value) in headers {
            request.setValue(value, forHTTPHeaderField: name)
        }
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await urlSession.data(for: request)
        } catch let error as URLError where error.code == .cancelled {
            throw CancellationError()
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            throw APIError.transport(wrapping: error)
        }
        guard let http = response as? HTTPURLResponse else { throw APIError.transport(URLError(.badServerResponse)) }
        guard (200..<300).contains(http.statusCode) else {
            throw APIError.from(status: http.statusCode, body: data, headerRequestId: http.value(forHTTPHeaderField: "X-Request-ID"))
        }
        return data
    }

    private struct ChallengeJSON: Decodable {
        let challengeId: UUID
        let challenge: String
    }

    /// `{"challenge_id": ..., "options": {...}}`. The options are WebAuthn's own camelCase JSON.
    private struct Ceremony<Options: Decodable>: Decodable {
        let challengeId: UUID
        let options: Options
    }

    private struct RegistrationJSON: Decodable {
        struct RelyingParty: Decodable { let id: String }
        struct User: Decodable {
            let id: String
            let name: String
        }

        let rp: RelyingParty
        let user: User
        let challenge: String
    }

    private struct AssertionJSON: Decodable {
        let rpId: String
        let challenge: String
    }
}

/// The WebAuthn JSON the server's verifier reads (`RegistrationResponseJSON`, `AuthenticationResponseJSON`),
/// built from what the platform authenticator returned. Binary fields are base64url without padding.
enum WebAuthnJSON {
    static func registration(_ passkey: PasskeyRegistration) -> [String: Any] {
        let id = passkey.credentialId.base64URLEncodedString()
        return [
            "id": id,
            "rawId": id,
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "clientExtensionResults": [String: Any](),
            "response": [
                "clientDataJSON": passkey.clientDataJSON.base64URLEncodedString(),
                "attestationObject": passkey.attestationObject.base64URLEncodedString(),
            ],
        ]
    }

    static func assertion(_ passkey: PasskeyAssertion) -> [String: Any] {
        let id = passkey.credentialId.base64URLEncodedString()
        var response: [String: Any] = [
            "clientDataJSON": passkey.clientDataJSON.base64URLEncodedString(),
            "authenticatorData": passkey.authenticatorData.base64URLEncodedString(),
            "signature": passkey.signature.base64URLEncodedString(),
        ]
        if let userId = passkey.userId, !userId.isEmpty {
            response["userHandle"] = userId.base64URLEncodedString()
        }
        return [
            "id": id,
            "rawId": id,
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "clientExtensionResults": [String: Any](),
            "response": response,
        ]
    }
}

nonisolated extension Data {
    /// Decodes base64url, with or without `=` padding.
    init?(base64URLEncoded string: String) {
        var base64 = string.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        base64 += String(repeating: "=", count: (4 - base64.count % 4) % 4)
        self.init(base64Encoded: base64)
    }

    /// base64url without padding, as WebAuthn writes it.
    func base64URLEncodedString() -> String {
        base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }
}
