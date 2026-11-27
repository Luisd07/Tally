import Foundation
import os
import Testing
@testable import Tally

/// A `TokenService` for tests that never refresh: any call is a test failure.
@MainActor
struct UnusedTokenService: TokenService {
    func refresh(serverURL: URL, refreshToken: String) async throws -> TokenPair {
        Issue.record("unexpected refresh")
        throw APIError.transport(URLError(.notConnectedToInternet))
    }

    func revoke(serverURL: URL, refreshToken: String) async throws {}
}

/// Answers refreshes from `answer`, holding each one until `release()` when `holds` is set, and logs every call.
@MainActor
final class FakeTokenService: TokenService {
    var answer: (String) throws -> TokenPair
    var holds = false
    private(set) var refreshed: [String] = []
    private(set) var revoked: [String] = []
    private var waiting: [CheckedContinuation<Void, Never>] = []

    init(answer: @escaping (String) throws -> TokenPair) {
        self.answer = answer
    }

    func refresh(serverURL: URL, refreshToken: String) async throws -> TokenPair {
        refreshed.append(refreshToken)
        if holds { await withCheckedContinuation { waiting.append($0) } }
        return try answer(refreshToken)
    }

    func revoke(serverURL: URL, refreshToken: String) async throws {
        revoked.append(refreshToken)
    }

    func release() {
        let waiting = waiting
        self.waiting = []
        waiting.forEach { $0.resume() }
    }
}

/// Stands in for the system passkey sheets.
@MainActor
final class FakePasskeys: PasskeyProviding {
    var registerError: (any Error)?
    var assertError: (any Error)?
    private(set) var registered: [PasskeyRegistrationOptions] = []
    private(set) var asserted: [PasskeyAssertionOptions] = []

    static let credentialId = Data([0xFB, 0xFF, 0x01, 0x02])

    func register(_ options: PasskeyRegistrationOptions) async throws -> PasskeyRegistration {
        registered.append(options)
        if let registerError { throw registerError }
        return PasskeyRegistration(
            credentialId: Self.credentialId, clientDataJSON: Data("client".utf8), attestationObject: Data([0xA0, 0x01])
        )
    }

    func assert(_ options: PasskeyAssertionOptions) async throws -> PasskeyAssertion {
        asserted.append(options)
        if let assertError { throw assertError }
        return PasskeyAssertion(
            credentialId: Self.credentialId,
            clientDataJSON: Data("client".utf8),
            authenticatorData: Data([0x01]),
            signature: Data([0x30, 0x45]),
            userId: Data([0x6F, 0x1C])
        )
    }
}

// MARK: - Encoding

@MainActor
struct WebAuthnJSONTests {
    @Test func base64URLRoundTripsWithoutPadding() throws {
        let bytes = Data([0xFB, 0xFF, 0xBF, 0x00, 0x01])
        let encoded = bytes.base64URLEncodedString()
        #expect(encoded == "-_-_AAE")
        #expect(Data(base64URLEncoded: encoded) == bytes)
        #expect(Data(base64URLEncoded: "AAE=") == Data([0x00, 0x01]))
        #expect(Data(base64URLEncoded: "not base64!") == nil)
    }

    @Test func registrationKeepsWebAuthnKeys() throws {
        let json = WebAuthnJSON.registration(
            PasskeyRegistration(credentialId: Data([0xFB, 0xFF]), clientDataJSON: Data("c".utf8), attestationObject: Data([0x01]))
        )
        #expect(json["id"] as? String == "-_8")
        #expect(json["rawId"] as? String == "-_8")
        #expect(json["type"] as? String == "public-key")
        let response = try #require(json["response"] as? [String: Any])
        #expect(response["clientDataJSON"] as? String == "Yw")
        #expect(response["attestationObject"] as? String == "AQ")
    }

    @Test func assertionCarriesUserHandleOnlyWhenPresent() throws {
        var passkey = PasskeyAssertion(
            credentialId: Data([0x01]), clientDataJSON: Data([0x02]), authenticatorData: Data([0x03]),
            signature: Data([0x04]), userId: Data([0x05])
        )
        var response = try #require(WebAuthnJSON.assertion(passkey)["response"] as? [String: Any])
        #expect(Set(response.keys) == ["clientDataJSON", "authenticatorData", "signature", "userHandle"])
        #expect(response["userHandle"] as? String == "BQ")

        passkey = PasskeyAssertion(
            credentialId: Data([0x01]), clientDataJSON: Data([0x02]), authenticatorData: Data([0x03]),
            signature: Data([0x04]), userId: nil
        )
        response = try #require(WebAuthnJSON.assertion(passkey)["response"] as? [String: Any])
        #expect(response["userHandle"] == nil)
    }
}

// MARK: - Session renewal

@MainActor
struct SessionRenewalTests {
    private let server = URL(string: Session.productionServerURL)!
    private let fixture = TestSession()

    @Test func signInStoresBothTokensAndRelaunchKeepsThem() throws {
        defer { try? fixture.cleanUp() }
        let tokens = FakeTokenService { _ in TokenPair(accessToken: "a2", refreshToken: "r2") }
        let session = fixture.make(tokens: tokens)
        try session.signIn(tokens: TokenPair(accessToken: "a1", refreshToken: "r1"), serverURL: server)
        #expect(try Keychain.read(fixture.tokenKey) == "a1")
        #expect(try Keychain.read("\(fixture.tokenKey).refresh") == "r1")
        #expect(fixture.make().accessToken == "a1")
    }

    @Test func renewRotatesAndStoresTheNewPair() async throws {
        defer { try? fixture.cleanUp() }
        let tokens = FakeTokenService { _ in TokenPair(accessToken: "a2", refreshToken: "r2") }
        let session = fixture.make(tokens: tokens)
        try session.signIn(tokens: TokenPair(accessToken: "a1", refreshToken: "r1"), serverURL: server)

        guard case .renewed = await session.renew(after: "a1") else { Issue.record("not renewed"); return }
        #expect(tokens.refreshed == ["r1"])
        #expect(session.accessToken == "a2")
        #expect(try Keychain.read(fixture.tokenKey) == "a2")
        #expect(try Keychain.read("\(fixture.tokenKey).refresh") == "r2")
    }

    @Test func concurrentRenewalsShareOneRefresh() async throws {
        defer { try? fixture.cleanUp() }
        let tokens = FakeTokenService { _ in TokenPair(accessToken: "a2", refreshToken: "r2") }
        tokens.holds = true
        let session = fixture.make(tokens: tokens)
        try session.signIn(tokens: TokenPair(accessToken: "a1", refreshToken: "r1"), serverURL: server)

        let first = Task { await session.renew(after: "a1") }
        let second = Task { await session.renew(after: "a1") }
        while tokens.refreshed.isEmpty { await Task.yield() }
        for _ in 0..<10 { await Task.yield() }
        tokens.release()

        for outcome in [await first.value, await second.value] {
            guard case .renewed = outcome else { Issue.record("not renewed"); return }
        }
        #expect(tokens.refreshed == ["r1"])
        #expect(session.accessToken == "a2")
    }

    @Test func rejectedRefreshSignsOut() async throws {
        defer { try? fixture.cleanUp() }
        let tokens = FakeTokenService { _ in
            throw APIError.from(status: 401, body: Data(), headerRequestId: nil)
        }
        let session = fixture.make(tokens: tokens)
        try session.signIn(tokens: TokenPair(accessToken: "a1", refreshToken: "r1"), serverURL: server)

        guard case .rejected = await session.renew(after: "a1") else { Issue.record("not rejected"); return }
        #expect(!session.isSignedIn)
        #expect(try Keychain.read("\(fixture.tokenKey).refresh") == nil)
    }

    @Test func unreachableServerKeepsTheSession() async throws {
        defer { try? fixture.cleanUp() }
        let tokens = FakeTokenService { _ in throw APIError.transport(URLError(.notConnectedToInternet)) }
        let session = fixture.make(tokens: tokens)
        try session.signIn(tokens: TokenPair(accessToken: "a1", refreshToken: "r1"), serverURL: server)

        guard case .failed(let error) = await session.renew(after: "a1") else { Issue.record("not failed"); return }
        #expect(error.status == nil)
        #expect(session.accessToken == "a1")
        #expect(try Keychain.read("\(fixture.tokenKey).refresh") == "r1")
    }

    @Test func devTokenCantBeRenewed() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "dev", serverURL: server)
        guard case .rejected = await session.renew(after: "dev") else { Issue.record("not rejected"); return }
        #expect(try Keychain.read("\(fixture.tokenKey).refresh") == nil)
    }

    @Test func signOutRevokesTheRefreshToken() async throws {
        defer { try? fixture.cleanUp() }
        let tokens = FakeTokenService { _ in TokenPair(accessToken: "a2", refreshToken: "r2") }
        let session = fixture.make(tokens: tokens)
        try session.signIn(tokens: TokenPair(accessToken: "a1", refreshToken: "r1"), serverURL: server)

        session.signOut()
        while tokens.revoked.isEmpty { await Task.yield() }
        #expect(tokens.revoked == ["r1"])
        #expect(try Keychain.read("\(fixture.tokenKey).refresh") == nil)
    }
}

// MARK: - Network (shares StubURLProtocol, so these run in the serialized suite)

extension APIClientTests {
    private var production: URL { URL(string: Session.productionServerURL)! }

    private func json(_ request: URLRequest) throws -> [String: Any] {
        let data = try #require(bodyData(request))
        return try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    @Test func authAPIReadsServerRegistrationOptions() async throws {
        let requests = reply(200, body: try jsonFixture("passkey_enroll_options"))
        let api = AuthAPI(serverURL: production, urlSession: StubURLProtocol.makeSession())

        let options = try await api.registrationOptions(code: "ABCD-EFGH-JKMN")
        #expect(options.challengeId == UUID(uuidString: "0b8c1f2e-3d4a-4b5c-8d6e-7f8091a2b3c4"))
        #expect(options.relyingPartyId == URL(string: Session.productionServerURL)!.host)
        #expect(options.challenge == Data(0..<32))
        #expect(options.userId == withUnsafeBytes(of: UUID(uuidString: "6f1c2b9e-0a4d-4e5f-9a8b-1c2d3e4f5a6b")!.uuid) { Data($0) })
        #expect(options.userName == "Luis")

        var iterator = requests.makeAsyncIterator()
        let request = try #require(await iterator.next())
        #expect(request.url?.path() == "/api/auth/enroll/options")
        #expect(request.value(forHTTPHeaderField: "Authorization") == nil)
        #expect(try json(request)["code"] as? String == "ABCD-EFGH-JKMN")
    }

    @Test func authAPISendsCredentialWithWebAuthnKeys() async throws {
        let requests = reply(200, body: Data(#"{"access_token":"a1","refresh_token":"r1","token_type":"bearer","expires_in":900}"#.utf8))
        let api = AuthAPI(serverURL: production, urlSession: StubURLProtocol.makeSession())
        let challengeId = UUID()
        let passkey = PasskeyAssertion(
            credentialId: Data([1]), clientDataJSON: Data([2]), authenticatorData: Data([3]), signature: Data([4]), userId: nil
        )

        let tokens = try await api.signIn(challengeId: challengeId, credential: WebAuthnJSON.assertion(passkey))
        #expect(tokens == TokenPair(accessToken: "a1", refreshToken: "r1"))

        var iterator = requests.makeAsyncIterator()
        let body = try json(try #require(await iterator.next()))
        #expect(body["challenge_id"] as? String == challengeId.uuidString)
        let response = try #require((body["credential"] as? [String: Any])?["response"] as? [String: Any])
        #expect(response["clientDataJSON"] as? String == "Ag")
        #expect(response["authenticatorData"] as? String == "Aw")
    }

    @Test func authAPIUnauthorizedIsAnAPIError() async throws {
        _ = reply(401, body: Data(#"{"error":{"code":"unauthorized","message":"Not signed in.","request_id":"r1"}}"#.utf8))
        let api = AuthAPI(serverURL: production, urlSession: StubURLProtocol.makeSession())
        let error = await #expect(throws: APIError.self) { _ = try await api.refresh("r1") }
        #expect(error?.isUnauthorized == true)
    }

    @Test func expiredAccessTokenRefreshesAndRetries() async throws {
        defer { try? fixture.cleanUp() }
        let tokens = FakeTokenService { _ in TokenPair(accessToken: "a2", refreshToken: "r2") }
        let session = fixture.make(tokens: tokens)
        try session.signIn(tokens: TokenPair(accessToken: "a1", refreshToken: "r1"), serverURL: server)
        let api = makeClient(session)
        let home = try jsonFixture("home-2026-10")
        let sent = OSAllocatedUnfairLock(initialState: [String]())
        StubURLProtocol.respond { request in
            let token = request.value(forHTTPHeaderField: "Authorization") ?? ""
            sent.withLock { $0.append(token) }
            let status = token == "Bearer a2" ? 200 : 401
            return (StubURLProtocol.response(to: request, status: status), status == 200 ? home : Data())
        }

        let _: Home = try await api.get("/api/home")
        #expect(sent.withLock { $0 } == ["Bearer a1", "Bearer a2"])
        #expect(tokens.refreshed == ["r1"])
        #expect(session.accessToken == "a2")
    }

    @Test func refreshRejectedSignsOutAfterOneRequest() async throws {
        defer { try? fixture.cleanUp() }
        let tokens = FakeTokenService { _ in throw APIError.from(status: 401, body: Data(), headerRequestId: nil) }
        let session = fixture.make(tokens: tokens)
        try session.signIn(tokens: TokenPair(accessToken: "a1", refreshToken: "r1"), serverURL: server)
        let api = makeClient(session)
        let count = OSAllocatedUnfairLock(initialState: 0)
        StubURLProtocol.respond { request in
            count.withLock { $0 += 1 }
            return (StubURLProtocol.response(to: request, status: 401), Data())
        }

        let error = await #expect(throws: APIError.self) { let _: Home = try await api.get("/api/home") }
        #expect(error?.isUnauthorized == true)
        #expect(count.withLock { $0 } == 1)
        #expect(!session.isSignedIn)
    }

    @Test func refreshOfflineFailsTheRequestButKeepsTheSession() async throws {
        defer { try? fixture.cleanUp() }
        let tokens = FakeTokenService { _ in throw APIError.transport(URLError(.notConnectedToInternet)) }
        let session = fixture.make(tokens: tokens)
        try session.signIn(tokens: TokenPair(accessToken: "a1", refreshToken: "r1"), serverURL: server)
        let api = makeClient(session)
        _ = reply(401)

        let error = await #expect(throws: APIError.self) { let _: Home = try await api.get("/api/home") }
        #expect(error?.status == nil)
        #expect(session.accessToken == "a1")
    }

    @Test func enrollmentCreatesPasskeyThenSignsIn() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        let passkeys = FakePasskeys()
        let model = PasskeySignInModel(
            passkeys: passkeys, api: AuthAPI(serverURL: production, urlSession: StubURLProtocol.makeSession())
        )
        let enroll = try jsonFixture("passkey_enroll_options")
        let signIn = try jsonFixture("passkey_signin_options")
        let paths = OSAllocatedUnfairLock(initialState: [String]())
        StubURLProtocol.respond { request in
            let path = request.url!.path()
            paths.withLock { $0.append(path) }
            let body: Data = switch path {
            case "/api/auth/enroll/options": enroll
            case "/api/auth/enroll/verify": Data(#"{"id":"\#(UUID())","device_name":"iPhone","created_at":"2026-10-24T00:00:00Z"}"#.utf8)
            case "/api/auth/signin/options": signIn
            default: Data(#"{"access_token":"a1","refresh_token":"r1","token_type":"bearer","expires_in":900}"#.utf8)
            }
            return (StubURLProtocol.response(to: request, status: path == "/api/auth/enroll/verify" ? 201 : 200), body)
        }

        await model.enroll(code: " abcd-efgh-jkmn ", deviceName: "Test iPhone", into: session)
        #expect(model.message == nil)
        #expect(paths.withLock { $0 } == [
            "/api/auth/enroll/options", "/api/auth/enroll/verify", "/api/auth/signin/options", "/api/auth/signin/verify",
        ])
        #expect(passkeys.registered.first?.relyingPartyId == URL(string: Session.productionServerURL)!.host)
        #expect(passkeys.asserted.first?.challenge == Data(32..<64))
        #expect(session.accessToken == "a1")
        #expect(session.serverURL == production)
    }

    @Test func badEnrollmentCodeExplainsItself() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        let passkeys = FakePasskeys()
        let model = PasskeySignInModel(
            passkeys: passkeys, api: AuthAPI(serverURL: production, urlSession: StubURLProtocol.makeSession())
        )
        _ = reply(401)

        await model.enroll(code: "WRONG", into: session)
        #expect(model.message?.contains("code didn't work") == true)
        #expect(passkeys.registered.isEmpty)
        #expect(!session.isSignedIn)
    }

    @Test func cancelledPasskeySheetShowsNoMessage() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        let passkeys = FakePasskeys()
        passkeys.assertError = PasskeyError.cancelled
        let model = PasskeySignInModel(
            passkeys: passkeys, api: AuthAPI(serverURL: production, urlSession: StubURLProtocol.makeSession())
        )
        _ = reply(200, body: try jsonFixture("passkey_signin_options"))

        await model.signIn(into: session)
        #expect(model.message == nil)
        #expect(!model.isWorking)
        #expect(!session.isSignedIn)
    }
}
