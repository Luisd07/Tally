import SwiftUI

/// Captures still on the phone: how many are waiting and why an earlier attempt didn't finish, each one the
/// server rejected with Retry and Discard, and any stored file that can't be read, with Discard.
/// Shows nothing when the outbox is empty.
struct UnsentCapturesSection: View {
    @Environment(Outbox.self) private var outbox
    @State private var actionFailed = false

    private var hasContent: Bool {
        !outbox.pending.isEmpty || !outbox.failed.isEmpty || !outbox.unreadable.isEmpty || outbox.storageProblem != nil
    }

    var body: some View {
        if hasContent {
            Section("Unsent captures") {
                if let problem = outbox.storageProblem {
                    Label("Captures can't be kept safely: \(problem)", systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.red)
                }
                if !outbox.pending.isEmpty {
                    if outbox.isFlushing {
                        Label("Sending…", systemImage: "arrow.up.circle")
                    } else {
                        Label("\(outbox.pending.count) waiting to send", systemImage: "clock")
                    }
                }
                ForEach(outbox.pending.filter { $0.lastError != nil }) { item in
                    waitingRow(item)
                }
                ForEach(outbox.failed) { item in
                    failedRow(item)
                }
                ForEach(outbox.unreadable, id: \.self) { id in
                    unreadableRow(id)
                }
            }
            .alert("Couldn't update the capture", isPresented: $actionFailed) {
                Button("OK", role: .cancel) {}
            }
        }
    }

    private func waitingRow(_ item: OutboxItem) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(item.capture.descriptionRaw)
            Text("Will try again: \(item.lastError ?? "")").font(.footnote).foregroundStyle(.secondary)
        }
    }

    private func failedRow(_ item: OutboxItem) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(item.capture.descriptionRaw)
            if item.rowSent {
                Text("Receipt not sent").font(.footnote.bold())
            }
            Text(item.failure ?? "").font(.footnote).foregroundStyle(.red)
            HStack {
                Button("Retry") {
                    do { try outbox.retry(item.id) } catch { actionFailed = true }
                }
                discardButton(item.rowSent ? "Discard receipt" : "Discard", id: item.id)
            }
            .buttonStyle(.borderless)
        }
    }

    private func unreadableRow(_ id: UUID) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("A saved capture can't be read")
            Text("Its file is damaged and can't be sent.").font(.footnote).foregroundStyle(.red)
            discardButton("Discard", id: id)
                .buttonStyle(.borderless)
        }
    }

    private func discardButton(_ title: String, id: UUID) -> some View {
        Button(title, role: .destructive) {
            do { try outbox.discard(id) } catch { actionFailed = true }
        }
    }
}
