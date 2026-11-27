import SwiftUI

/// SimpleFIN Bridge: paste a setup token to connect the banks, then sync now or see how recent syncs went.
struct BankConnectionView: View {
    @Environment(APIClient.self) private var api
    @State private var model = BankConnectionModel()
    @State private var setupToken = ""

    var body: some View {
        List {
            if let message = model.message {
                Section { Text(message).foregroundStyle(.red) }
            }
            if let host = model.connectedHost {
                Section {
                    Label("Connected to \(host)", systemImage: "checkmark.circle.fill")
                        .foregroundStyle(Color(.moneyIn))
                }
            }
            Section {
                SecureField("Setup token", text: $setupToken)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                Button("Connect") {
                    Task {
                        if await model.connect(setupToken: setupToken, api: api) { setupToken = "" }
                    }
                }
                .disabled(model.isWorking || setupToken.trimmingCharacters(in: .whitespaces).isEmpty)
            } header: {
                Text("Connect SimpleFIN")
            } footer: {
                Text("In SimpleFIN Bridge, create a setup token for Tally and paste it here. It works once. Connecting again with a new token replaces the old connection.")
            }
            Section {
                Button("Sync now") { Task { await model.syncNow(api: api) } }
                    .disabled(model.isWorking)
            } footer: {
                Text("Tally also syncs every night. The first sync brings in this year's transactions.")
            }
            Section("Recent syncs") {
                if model.hasLoaded && model.runs.isEmpty {
                    Text("No syncs yet").foregroundStyle(.secondary)
                }
                ForEach(model.runs) { run in
                    SyncRunRow(run: run)
                }
            }
        }
        .navigationTitle("Bank connection")
        .overlay { if model.isWorking { ProgressView() } }
        .task { await model.load(api: api) }
        .refreshable { await model.load(api: api) }
    }
}

private struct SyncRunRow: View {
    let run: SyncRun

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Label(statusText, systemImage: statusSymbol)
                    .foregroundStyle(statusColor)
                Spacer()
                Text(run.startedAt.formatted(date: .abbreviated, time: .shortened))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Text("\(run.newCount) new, \(run.updatedCount) rechecked")
                .font(.caption)
                .foregroundStyle(.secondary)
            ForEach(Array(run.errors.enumerated()), id: \.offset) { _, problem in
                if let msg = problem.msg {
                    Text(msg).font(.caption).foregroundStyle(Color(.attention))
                }
            }
        }
    }

    private var statusText: String {
        switch run.status {
        case .running: "Running"
        case .ok: "Synced"
        case .partial: "Partly synced"
        case .failed: "Failed"
        case .other(let raw): raw
        }
    }

    private var statusSymbol: String {
        switch run.status {
        case .ok: "checkmark.circle"
        case .running: "arrow.triangle.2.circlepath"
        case .partial, .failed, .other: "exclamationmark.triangle"
        }
    }

    private var statusColor: Color {
        switch run.status {
        case .ok, .running: .primary
        case .partial, .failed, .other: Color(.attention)
        }
    }
}
