import SwiftUI

/// Every passkey sign-in, enrollment and refused attempt the server recorded (#22).
struct SignInActivityView: View {
    @Environment(APIClient.self) private var api
    @State private var model = SignInActivityModel()

    var body: some View {
        List {
            if let message = model.message {
                Section { Text(message).foregroundStyle(.red) }
            }
            Section {
                Toggle("Show session renewals", isOn: $model.showsRenewals)
            } footer: {
                Text("The app renews its session about every 15 minutes while you use it. Refused attempts are shown in orange.")
            }
            Section {
                if model.hasLoaded && model.visible.isEmpty {
                    Text("No activity yet").foregroundStyle(.secondary)
                }
                ForEach(model.visible) { event in
                    AuthEventRow(event: event)
                }
                if model.hasMore {
                    Button("Load more") { Task { await model.loadMore(api: api) } }
                        .disabled(model.isLoadingMore)
                }
            }
        }
        .navigationTitle("Sign-in activity")
        .overlay { if !model.hasLoaded { ProgressView() } }
        .task { await model.load(api: api) }
        .refreshable { await model.load(api: api) }
    }
}

private struct AuthEventRow: View {
    let event: AuthEvent

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Label(event.kind.label, systemImage: event.kind.symbol)
                    .foregroundStyle(event.kind.isFailure ? Color(.attention) : .primary)
                Spacer()
                Text(event.createdAt.formatted(date: .abbreviated, time: .shortened))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if let detail {
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .accessibilityElement(children: .combine)
    }

    /// "203.0.113.5 · Tally/1 CFNetwork/…": where the request came from.
    private var detail: String? {
        let parts = [event.ip, event.userAgent].compactMap { $0 }.filter { !$0.isEmpty }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }
}
