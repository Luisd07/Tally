import Foundation

#if DEBUG
/// Credentials for a token that isn't signed in yet, so a rejection can't touch the real `Session`.
private final class CandidateCredentials: APICredentialsSource {
    let serverURL: URL
    let accessToken: String?

    init(serverURL: URL, accessToken: String) {
        self.serverURL = serverURL
        self.accessToken = accessToken
    }

    func tokenRejected(_ token: String) {}
}

enum DevSignInValidator {
    /// Asks the server for the current month with the candidate token.
    /// Returns nil when it is accepted, otherwise a message for the user.
    static func validate(token: String, serverURL: URL, urlSession: URLSession = APIClient.defaultSession) async -> String? {
        let credentials = CandidateCredentials(serverURL: serverURL, accessToken: token)
        let client = APIClient(credentials: credentials, urlSession: urlSession)
        do {
            _ = try await client.home(month: MonthRef())
            return nil
        } catch is CancellationError {
            return "Sign-in was cancelled."
        } catch {
            return error.localizedDescription
        }
    }
}
#endif
