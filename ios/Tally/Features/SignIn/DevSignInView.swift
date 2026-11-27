import SwiftUI

#if DEBUG
/// Debug-only sign-in: paste a token from `scripts.dev_token` and point the app at a server.
struct DevSignInView: View {
    @Environment(Session.self) private var session
    @State private var token = ""
    @State private var serverURL = Session.defaultServerURL
    @State private var isSigningIn = false
    @State private var errorMessage: String?

    var body: some View {
        Form {
            Section("Server") {
                TextField("Server URL", text: $serverURL)
                    .textInputAutocapitalization(.never)
                    .keyboardType(.URL)
                    .autocorrectionDisabled()
            }
            Section("Dev token") {
                SecureField("Access token", text: $token)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
            }
            if let errorMessage {
                Section { Text(errorMessage).foregroundStyle(.red) }
            }
            Section {
                Button("Sign In") { Task { await signIn() } }
                    .disabled(isSigningIn || token.isEmpty)
            }
        }
        .navigationTitle("Dev sign-in")
        .onAppear { serverURL = session.serverURL.absoluteString }
    }

    private func signIn() async {
        guard let url = URL(string: serverURL.trimmingCharacters(in: .whitespaces)), url.host() != nil else {
            errorMessage = "Enter a valid server URL."
            return
        }
        isSigningIn = true
        errorMessage = nil
        defer { isSigningIn = false }
        let candidate = token.trimmingCharacters(in: .whitespacesAndNewlines)
        if let rejection = await DevSignInValidator.validate(token: candidate, serverURL: url) {
            errorMessage = rejection
            return
        }
        do {
            try session.signIn(token: candidate, serverURL: url)
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}
#endif
