import SwiftUI

/// Shown while signed out: sign in with this iPhone's passkey, or set one up with an enrollment code.
/// DEBUG builds also link to the dev-token sign-in.
struct SignInView: View {
    @Environment(Session.self) private var session
    @State private var model = PasskeySignInModel()
    @State private var showsEnrollment = false

    var body: some View {
        NavigationStack {
            VStack(spacing: 12) {
                Spacer()
                Image(systemName: "person.badge.key.fill")
                    .font(.system(size: 56))
                    .foregroundStyle(.tint)
                Text("Tally")
                    .font(.largeTitle.bold())
                Text("Sign in with the passkey on this iPhone.")
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                Spacer()
                if let message = model.message, !showsEnrollment {
                    Text(message)
                        .font(.footnote)
                        .foregroundStyle(.red)
                        .multilineTextAlignment(.center)
                }
                Button {
                    Task { await model.signIn(into: session) }
                } label: {
                    Label("Sign in with passkey", systemImage: "person.badge.key")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                Button("Set up this iPhone") { showsEnrollment = true }
                    .padding(.vertical, 8)
            }
            .padding()
            .disabled(model.isWorking)
            .overlay { if model.isWorking { ProgressView() } }
            .sheet(isPresented: $showsEnrollment) {
                EnrollmentSheet(model: model)
            }
            #if DEBUG
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    NavigationLink("Developer") { DevSignInView() }
                }
            }
            #endif
        }
    }
}

/// Enter the one-time code from `scripts.enroll` to create this iPhone's passkey.
private struct EnrollmentSheet: View {
    let model: PasskeySignInModel
    @Environment(Session.self) private var session
    @Environment(\.dismiss) private var dismiss
    @State private var code = ""

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("XXXX-XXXX-XXXX", text: $code)
                        .textInputAutocapitalization(.characters)
                        .autocorrectionDisabled()
                        .font(.body.monospaced())
                        .onSubmit(enroll)
                } header: {
                    Text("Enrollment code")
                } footer: {
                    Text("On the server, run `docker compose exec api python -m scripts.enroll` and enter the code it prints. It works once, for 10 minutes.")
                }
                if let message = model.message {
                    Section { Text(message).foregroundStyle(.red) }
                }
                Section {
                    Button("Create passkey", action: enroll)
                        .disabled(model.isWorking || code.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .navigationTitle("Set up this iPhone")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            }
            .overlay { if model.isWorking { ProgressView() } }
        }
        .interactiveDismissDisabled(model.isWorking)
    }

    private func enroll() {
        Task { await model.enroll(code: code, into: session) }
    }
}
