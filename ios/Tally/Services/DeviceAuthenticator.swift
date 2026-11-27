import LocalAuthentication

enum AuthResult: Equatable {
    case success
    case cancelled
    case passcodeNotSet
    case failed(String)
}

protocol Authenticator {
    func authenticate() async -> AuthResult
}

/// Face ID, Touch ID or the device passcode, through `LAContext`.
struct LiveAuthenticator: Authenticator {
    func authenticate() async -> AuthResult {
        let context = LAContext()
        var error: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &error) else {
            if let error, error.code == LAError.passcodeNotSet.rawValue {
                return .passcodeNotSet
            }
            return .failed(error?.localizedDescription ?? "Couldn't authenticate.")
        }
        do {
            _ = try await context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: "Unlock Tally")
            return .success
        } catch let error as LAError where error.code == .userCancel || error.code == .appCancel
            || error.code == .systemCancel {
            return .cancelled
        } catch {
            return .failed(error.localizedDescription)
        }
    }
}
