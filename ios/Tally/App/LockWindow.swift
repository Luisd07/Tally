import SwiftUI
import UIKit

/// Shows the lock screen and privacy cover in their own window above alerts, so no sheet, alert,
/// share sheet or photo picker can sit on top of them. Hidden, the window takes no touches.
@MainActor
final class LockWindow {
    private weak var scene: UIWindowScene?
    private var window: UIWindow?
    private var shownLock: AppLock?
    var userInterfaceStyle: UIUserInterfaceStyle = .unspecified {
        didSet { window?.overrideUserInterfaceStyle = userInterfaceStyle }
    }

    func attach(to scene: UIWindowScene) {
        self.scene = scene
        if let shownLock { show(shownLock) }
    }

    func show(_ lock: AppLock) {
        shownLock = lock
        guard let scene else { return }
        if window?.windowScene !== scene {
            let window = UIWindow(windowScene: scene)
            window.windowLevel = .alert + 1
            window.overrideUserInterfaceStyle = userInterfaceStyle
            window.rootViewController = UIHostingController(rootView: LockOverlay(lock: lock))
            self.window = window
        }
        window?.isHidden = false
    }

    func hide() {
        shownLock = nil
        window?.isHidden = true
    }

    func tearDown() {
        hide()
        window = nil
    }
}

private struct LockOverlay: View {
    let lock: AppLock

    var body: some View {
        ZStack {
            Color(.systemBackground).ignoresSafeArea()
            if lock.isLocked {
                LockView(lock: lock)
            }
        }
    }
}

/// Reports the window scene the view is placed in.
struct WindowSceneReader: UIViewRepresentable {
    let onAttach: (UIWindowScene) -> Void

    func makeUIView(context: Context) -> SceneView {
        let view = SceneView()
        view.onAttach = onAttach
        return view
    }

    func updateUIView(_ view: SceneView, context: Context) {
        view.onAttach = onAttach
    }

    final class SceneView: UIView {
        var onAttach: ((UIWindowScene) -> Void)?

        override func didMoveToWindow() {
            super.didMoveToWindow()
            if let scene = window?.windowScene { onAttach?(scene) }
        }
    }
}
