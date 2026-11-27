import AuthenticationServices
import UIKit

/// A passkey the platform just created.
nonisolated struct PasskeyRegistration: Sendable {
    let credentialId: Data
    let clientDataJSON: Data
    let attestationObject: Data
}

/// A passkey's signature over a sign-in challenge.
nonisolated struct PasskeyAssertion: Sendable {
    let credentialId: Data
    let clientDataJSON: Data
    let authenticatorData: Data
    let signature: Data
    let userId: Data?
}

enum PasskeyError: Error, LocalizedError {
    /// The user closed the passkey sheet. Not worth a message.
    case cancelled
    /// Another passkey request is already showing.
    case busy
    /// The platform answered with something other than a passkey credential.
    case unexpectedCredential
    /// The platform refused, e.g. the app isn't associated with the server's domain.
    case failed(String)

    var errorDescription: String? {
        switch self {
        case .cancelled: "Cancelled."
        case .busy: "A passkey request is already open."
        case .unexpectedCredential: "iOS returned something other than a passkey."
        case .failed(let message): message
        }
    }
}

/// Creates and uses passkeys. `PasskeyAuthenticator` is the real one; tests stand in their own.
protocol PasskeyProviding {
    func register(_ options: PasskeyRegistrationOptions) async throws -> PasskeyRegistration
    func assert(_ options: PasskeyAssertionOptions) async throws -> PasskeyAssertion
}

/// The system passkey sheets, through `ASAuthorizationController`.
final class PasskeyAuthenticator: NSObject, PasskeyProviding {
    private var continuation: CheckedContinuation<ASAuthorization, any Error>?
    /// Held while a request runs: the controller doesn't keep itself alive.
    private var controller: ASAuthorizationController?

    func register(_ options: PasskeyRegistrationOptions) async throws -> PasskeyRegistration {
        let provider = ASAuthorizationPlatformPublicKeyCredentialProvider(relyingPartyIdentifier: options.relyingPartyId)
        let request = provider.createCredentialRegistrationRequest(
            challenge: options.challenge, name: options.userName, userID: options.userId
        )
        let authorization = try await perform(request)
        guard let credential = authorization.credential as? ASAuthorizationPlatformPublicKeyCredentialRegistration,
              let attestationObject = credential.rawAttestationObject
        else { throw PasskeyError.unexpectedCredential }
        return PasskeyRegistration(
            credentialId: credential.credentialID,
            clientDataJSON: credential.rawClientDataJSON,
            attestationObject: attestationObject
        )
    }

    func assert(_ options: PasskeyAssertionOptions) async throws -> PasskeyAssertion {
        let provider = ASAuthorizationPlatformPublicKeyCredentialProvider(relyingPartyIdentifier: options.relyingPartyId)
        let request = provider.createCredentialAssertionRequest(challenge: options.challenge)
        let authorization = try await perform(request)
        guard let credential = authorization.credential as? ASAuthorizationPlatformPublicKeyCredentialAssertion else {
            throw PasskeyError.unexpectedCredential
        }
        return PasskeyAssertion(
            credentialId: credential.credentialID,
            clientDataJSON: credential.rawClientDataJSON,
            authenticatorData: credential.rawAuthenticatorData,
            signature: credential.signature,
            userId: credential.userID
        )
    }

    private func perform(_ request: ASAuthorizationRequest) async throws -> ASAuthorization {
        guard continuation == nil else { throw PasskeyError.busy }
        return try await withCheckedThrowingContinuation { continuation in
            self.continuation = continuation
            let controller = ASAuthorizationController(authorizationRequests: [request])
            controller.delegate = self
            controller.presentationContextProvider = self
            self.controller = controller
            controller.performRequests()
        }
    }

    private func finish(_ result: Result<ASAuthorization, any Error>) {
        let continuation = continuation
        self.continuation = nil
        controller = nil
        continuation?.resume(with: result)
    }

    /// `.canceled` covers both the user closing the sheet and there being no passkey to offer.
    static func translate(_ error: any Error) -> PasskeyError {
        if let error = error as? ASAuthorizationError, error.code == .canceled { return .cancelled }
        return .failed(error.localizedDescription)
    }
}

extension PasskeyAuthenticator: ASAuthorizationControllerDelegate {
    func authorizationController(controller: ASAuthorizationController, didCompleteWithAuthorization authorization: ASAuthorization) {
        finish(.success(authorization))
    }

    func authorizationController(controller: ASAuthorizationController, didCompleteWithError error: any Error) {
        finish(.failure(Self.translate(error)))
    }
}

extension PasskeyAuthenticator: ASAuthorizationControllerPresentationContextProviding {
    func presentationAnchor(for controller: ASAuthorizationController) -> ASPresentationAnchor {
        let windows = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.flatMap(\.windows)
        return windows.first(where: \.isKeyWindow) ?? windows.first ?? ASPresentationAnchor()
    }
}
