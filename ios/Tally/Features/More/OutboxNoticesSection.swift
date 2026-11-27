import SwiftUI

/// Captures that were sent but not exactly as picked, each with a Dismiss. Shows nothing when there are none.
struct OutboxNoticesSection: View {
    @Environment(Outbox.self) private var outbox

    var body: some View {
        if !outbox.notices.isEmpty {
            Section("Notices") {
                ForEach(outbox.notices) { notice in
                    VStack(alignment: .leading, spacing: 8) {
                        Text(notice.title)
                        Text(notice.message).font(.footnote).foregroundStyle(.secondary)
                        Button("Dismiss") { outbox.dismissNotice(notice.id) }
                            .buttonStyle(.borderless)
                    }
                }
            }
        }
    }
}
