import SwiftUI
import VisionKit

/// VisionKit's document camera (it finds the receipt's edges, crops and straightens it) wrapped for SwiftUI.
/// The scanned pages stay in memory and go straight to `onFinish`; nothing is written to disk.
struct ReceiptScanner: UIViewControllerRepresentable {
    enum Outcome {
        /// Every page of the scan, in order.
        case scanned([CGImage])
        case cancelled
        case failed
    }

    /// False where there's no document camera (the simulator), so callers hide the scan option.
    static var isSupported: Bool { VNDocumentCameraViewController.isSupported }

    let onFinish: (Outcome) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onFinish: onFinish) }

    func makeUIViewController(context: Context) -> VNDocumentCameraViewController {
        let controller = VNDocumentCameraViewController()
        controller.delegate = context.coordinator
        return controller
    }

    func updateUIViewController(_ controller: VNDocumentCameraViewController, context: Context) {
        context.coordinator.onFinish = onFinish
    }

    final class Coordinator: NSObject, VNDocumentCameraViewControllerDelegate {
        var onFinish: (Outcome) -> Void

        init(onFinish: @escaping (Outcome) -> Void) {
            self.onFinish = onFinish
        }

        func documentCameraViewController(_ controller: VNDocumentCameraViewController, didFinishWith scan: VNDocumentCameraScan) {
            let pages = (0..<scan.pageCount).compactMap { scan.imageOfPage(at: $0).cgImage }
            onFinish(pages.isEmpty ? .failed : .scanned(pages))
        }

        func documentCameraViewControllerDidCancel(_ controller: VNDocumentCameraViewController) {
            onFinish(.cancelled)
        }

        func documentCameraViewController(_ controller: VNDocumentCameraViewController, didFailWithError error: Error) {
            onFinish(.failed)
        }
    }
}

extension View {
    /// Presents the document camera full screen while `isPresented` is true; it closes before `onScan` runs.
    func receiptScanner(isPresented: Binding<Bool>, onScan: @escaping ([CGImage]) -> Void, onFailure: @escaping () -> Void) -> some View {
        fullScreenCover(isPresented: isPresented) {
            ReceiptScanner { outcome in
                isPresented.wrappedValue = false
                switch outcome {
                case .scanned(let pages): onScan(pages)
                case .failed: onFailure()
                case .cancelled: break
                }
            }
            .ignoresSafeArea()
        }
    }
}
