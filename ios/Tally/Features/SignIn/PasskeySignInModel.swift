import Foundation
import Observation
import UIKit

/// Enrolling this iPhone's passkey with a one-time code, and signing in with it.
@Observable
final class PasskeySignInModel {
    private(set) var isWorking = false
    /// What went wrong, for the screen; nil after a success or a cancelled sheet.
    private(set) var message: String?

    private let passkeys: any PasskeyProviding
    private let api: AuthAPI

    init(
        passkeys: any PasskeyProviding = PasskeyAuthenticator(),
        api: AuthAPI = AuthAPI(serverURL: URL(string: Session.productionServerURL)!, attestation: .shared)
    ) {
        self.passkeys = passkeys
        self.api = api
    }

    func signIn(into session: Session) async {
        await run(.signIn) { try await self.signInSteps(session) }
    }

    /// Creates a passkey with the code from `scripts.enroll`, then signs in with it.
    func enroll(code: String, deviceName: String = UIDevice.current.name, into session: Session) async {
        let code = code.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !code.isEmpty else { return }
        await run(.enroll) {
            let options = try await self.api.registrationOptions(code: code)
            let passkey = try await self.passkeys.register(options)
            try await self.api.register(
                challengeId: options.challengeId,
                credential: WebAuthnJSON.registration(passkey),
                deviceName: deviceName
            )
            try await self.signInSteps(session)
        }
    }

    private func signInSteps(_ session: Session) async throws {
        let options = try await api.assertionOptions()
        let passkey = try await passkeys.assert(options)
        let tokens = try await api.signIn(challengeId: options.challengeId, credential: WebAuthnJSON.assertion(passkey))
        try session.signIn(tokens: tokens, serverURL: api.serverURL)
    }

    enum Flow { case signIn, enroll }

    private func run(_ flow: Flow, _ steps: @escaping () async throws -> Void) async {
        guard !isWorking else { return }
        isWorking = true
        message = nil
        defer { isWorking = false }
        do {
            try await steps()
        } catch {
            message = Self.message(for: error, in: flow)
        }
    }

    static func message(for error: any Error, in flow: Flow) -> String? {
        switch error {
        case PasskeyError.cancelled, is CancellationError:
            return nil
        case let error as APIError where error.isUnauthorized:
            switch flow {
            case .enroll: return "That code didn't work. It may be mistyped, expired (10 minutes) or already used. Run the enroll script for a new one."
            case .signIn: return "The server didn't accept this passkey. Set up this iPhone again with a new enrollment code."
            }
        default:
            return error.localizedDescription
        }
    }
}
