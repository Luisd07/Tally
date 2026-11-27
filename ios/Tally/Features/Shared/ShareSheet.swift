import SwiftUI
import UIKit

/// The system share sheet (AirDrop, Files, Mail) for `items`; `onDone` runs when it completes or is cancelled.
struct ShareSheet: UIViewControllerRepresentable {
    let items: [Any]
    let onDone: () -> Void

    func makeUIViewController(context: Context) -> UIActivityViewController {
        let controller = UIActivityViewController(activityItems: items, applicationActivities: nil)
        controller.completionWithItemsHandler = { _, _, _, _ in onDone() }
        return controller
    }

    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}
