import SwiftUI

/// A bar saying what just happened, with an Undo button. It calls `expire` once it has shown for `duration`
/// without being undone; give each action its own `id` so a new one shows for the full time.
struct UndoBanner: View {
    let message: String
    var duration: Duration = .seconds(5)
    let undo: () -> Void
    let expire: () -> Void

    var body: some View {
        HStack {
            Text(message).lineLimit(2)
            Spacer()
            Button("Undo", action: undo).bold()
        }
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.regularMaterial)
        .task {
            guard (try? await Task.sleep(for: duration)) != nil else { return }
            expire()
        }
    }
}
