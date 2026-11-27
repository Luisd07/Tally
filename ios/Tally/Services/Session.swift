import Foundation
import Observation
import os

/// How a 401 was resolved: a new access token to retry with, the session ended, or the refresh couldn't be
/// tried (offline, server down), which keeps the session and fails just this request.
enum TokenRenewal {
    case renewed
    case rejected
    case failed(APIError)
}

/// The refresh and revoke calls `Session` makes. `AuthTokenService` is the real one; tests stand in their own.
protocol TokenService {
    func refresh(serverURL: URL, refreshToken: String) async throws -> TokenPair
    func revoke(serverURL: URL, refreshToken: String) async throws
}

struct AuthTokenService: TokenService {
    func refresh(serverURL: URL, refreshToken: String) async throws -> TokenPair {
        try await AuthAPI(serverURL: serverURL, attestation: .shared).refresh(refreshToken)
    }

    func revoke(serverURL: URL, refreshToken: String) async throws {
        try await AuthAPI(serverURL: serverURL).signOut(refreshToken)
    }
}

/// The signed-in state: the access and refresh tokens (Keychain) and the server URL (UserDefaults).
/// `APIClient` reads the access token from here, and on a 401 asks `renew(after:)` for a new one.
@Observable
final class Session: APICredentialsSource {
    /// The server passkeys belong to. A passkey only works against the domain in the app's associated domains,
    /// so passkey sign-in always uses this one, in DEBUG too.
    /// `api.example.com` is a placeholder: to run your own instance, set your domain here and in
    /// `ios/Config/Tally.entitlements` (`webcredentials:`).
    static let productionServerURL = "https://api.example.com"

    #if DEBUG
    static let defaultServerURL = "http://127.0.0.1:8000"
    #else
    static let defaultServerURL = productionServerURL
    #endif

    private static let serverURLKey = "server_url"
    private static let pendingDeleteKey = "token_delete_pending"
    private static let logger = Logger(subsystem: "com.luisd07.tally", category: "Session")

    private let tokenKey: String
    private let refreshKey: String
    private let defaults: UserDefaults
    private let tokens: any TokenService

    private(set) var accessToken: String? = nil
    /// Nil for a dev token, which can't be refreshed.
    @ObservationIgnored private var refreshToken: String? = nil
    private(set) var serverURL: URL
    /// The most recent Keychain failure, for screens to surface.
    private(set) var lastError: (any Error)? = nil
    /// The refresh in flight. Every 401 waits on this one: the server treats a reused refresh token as theft.
    @ObservationIgnored private var renewal: Task<TokenRenewal, Never>?

    var isSignedIn: Bool { accessToken != nil }

    /// `tokenKey` and `defaults` let tests use their own Keychain items and settings; the refresh token is
    /// stored under `"<tokenKey>.refresh"`.
    init(tokenKey: String = "access_token", defaults: UserDefaults = .standard, tokens: any TokenService = AuthTokenService()) {
        self.tokenKey = tokenKey
        self.refreshKey = "\(tokenKey).refresh"
        self.defaults = defaults
        self.tokens = tokens
        let stored = defaults.string(forKey: Self.serverURLKey)
        serverURL = URL(string: stored ?? Self.defaultServerURL) ?? URL(string: Self.defaultServerURL)!
        loadTokens()
        #if DEBUG
        seedFromEnvironment()
        #endif
    }

    /// Signs in with a passkey session's tokens; throws if the Keychain refuses them.
    func signIn(tokens pair: TokenPair, serverURL: URL) throws {
        try store(access: pair.accessToken, refresh: pair.refreshToken)
        finishSignIn(serverURL: serverURL)
    }

    /// Signs in with an access token alone (a dev token), which ends the session when it expires.
    func signIn(token: String, serverURL: URL) throws {
        try store(access: token, refresh: nil)
        finishSignIn(serverURL: serverURL)
    }

    /// Signs out now and revokes the refresh token on the server in the background. If the Keychain delete
    /// fails, the failure is recorded and retried on next launch.
    func signOut() {
        let revoked = refreshToken
        let server = serverURL
        accessToken = nil
        refreshToken = nil
        CapturePreferences(defaults: defaults).clear()
        GettingStartedPreferences(defaults: defaults).clear()
        do {
            try Keychain.delete(tokenKey)
            try Keychain.delete(refreshKey)
        } catch {
            defaults.set(true, forKey: Self.pendingDeleteKey)
            record(error, "Keychain delete failed; retrying on next launch")
        }
        if let revoked {
            let tokens = tokens
            Task { try? await tokens.revoke(serverURL: server, refreshToken: revoked) }
        }
    }

    /// Signs out only when `token` is still current, so a stale 401 can't end a newer session.
    func tokenRejected(_ token: String) {
        guard token == accessToken else { return }
        signOut()
    }

    /// A new access token after `rejected` got a 401. Concurrent callers share one refresh.
    func renew(after rejected: String) async -> TokenRenewal {
        guard let current = accessToken else { return .rejected }
        if current != rejected { return .renewed }
        if let renewal { return await renewal.value }
        guard let refresh = refreshToken else { return .rejected }
        let task = Task { await refreshTokens(from: refresh) }
        renewal = task
        let outcome = await task.value
        renewal = nil
        return outcome
    }

    private func refreshTokens(from refresh: String) async -> TokenRenewal {
        do {
            let pair = try await tokens.refresh(serverURL: serverURL, refreshToken: refresh)
            // Signed out or in again while the refresh ran: that session wins.
            guard refreshToken == refresh else { return isSignedIn ? .renewed : .rejected }
            do {
                try store(access: pair.accessToken, refresh: pair.refreshToken)
            } catch {
                // Keep the new pair in memory: the old refresh token is already spent.
                accessToken = pair.accessToken
                refreshToken = pair.refreshToken
                record(error, "Couldn't store refreshed tokens")
            }
            return .renewed
        } catch let error as APIError where error.isUnauthorized {
            if refreshToken == refresh { signOut() }
            return .rejected
        } catch {
            return .failed(APIError(wrapping: error))
        }
    }

    /// Saves both tokens, then updates memory. A nil `refresh` clears any stored refresh token.
    private func store(access: String, refresh: String?) throws {
        if let refresh {
            try Keychain.save(refresh, for: refreshKey)
        } else {
            try Keychain.delete(refreshKey)
        }
        try Keychain.save(access, for: tokenKey)
        accessToken = access
        refreshToken = refresh
    }

    private func finishSignIn(serverURL: URL) {
        defaults.removeObject(forKey: Self.pendingDeleteKey)
        defaults.set(serverURL.absoluteString, forKey: Self.serverURLKey)
        self.serverURL = serverURL
        lastError = nil
    }

    /// The stored tokens, after finishing any sign-out whose Keychain delete failed last time.
    private func loadTokens() {
        do {
            if defaults.bool(forKey: Self.pendingDeleteKey) {
                try Keychain.delete(tokenKey)
                try Keychain.delete(refreshKey)
                defaults.removeObject(forKey: Self.pendingDeleteKey)
                return
            }
            accessToken = try Keychain.read(tokenKey)
            refreshToken = accessToken == nil ? nil : try Keychain.read(refreshKey)
        } catch {
            accessToken = nil
            refreshToken = nil
            record(error, "Keychain read failed; starting signed out")
        }
    }

    private func record(_ error: any Error, _ context: String) {
        lastError = error
        Self.logger.error("\(context, privacy: .public): \(error.localizedDescription, privacy: .public)")
    }

    #if DEBUG
    /// Lets `SIMCTL_CHILD_TALLY_DEV_TOKEN=... xcrun simctl launch` start the app signed in.
    private func seedFromEnvironment() {
        let env = ProcessInfo.processInfo.environment
        guard let token = env["TALLY_DEV_TOKEN"], !token.isEmpty else { return }
        let url = env["TALLY_SERVER_URL"].flatMap { URL(string: $0) } ?? serverURL
        do {
            try signIn(token: token, serverURL: url)
        } catch {
            record(error, "Couldn't store TALLY_DEV_TOKEN")
        }
    }
    #endif
}
