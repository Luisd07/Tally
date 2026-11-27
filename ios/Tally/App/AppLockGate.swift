import SwiftUI

struct LockView: View {
    let lock: AppLock

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "lock.fill")
                .font(.system(size: 48))
                .foregroundStyle(.tint)
            Text("Tally")
                .font(.largeTitle.bold())
            if let message = lock.message {
                Text(text(for: message))
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
            Button("Unlock") { Task { await lock.unlock() } }
                .buttonStyle(.borderedProminent)
        }
        .padding()
    }

    private func text(for message: LockMessage) -> String {
        switch message {
        case .passcodeRequired: "Set a passcode in Settings to open Tally"
        case .failed(let text): text
        }
    }
}

/// Picks signed-in or signed-out content and drives the lock from the session and scene phase.
/// The lock screen and cover live in `LockWindow`; signed-in content mounts after the first unlock
/// and then stays mounted across re-locks, keeping tabs, sheets and drafts.
struct AppLockGate<SignedIn: View, SignedOut: View>: View {
    let lock: AppLock
    @ViewBuilder let signedIn: SignedIn
    @ViewBuilder let signedOut: SignedOut
    @Environment(Session.self) private var session
    @Environment(AppearanceStore.self) private var appearance
    @Environment(\.scenePhase) private var scenePhase
    @State private var lockWindow = LockWindow()

    var body: some View {
        Group {
            if !session.isSignedIn {
                signedOut
            } else if lock.hasUnlocked {
                signedIn
            } else {
                Color(.systemBackground).ignoresSafeArea()
            }
        }
        .background(WindowSceneReader {
            lockWindow.attach(to: $0)
            appearance.preference.apply(to: $0.windows)
        })
        .onChange(of: appearance.preference, initial: true) { _, preference in
            lockWindow.userInterfaceStyle = preference.userInterfaceStyle
            preference.applyToAppWindows()
        }
        .onChange(of: session.isSignedIn, initial: true) { _, signedIn in
            lock.sessionChanged(signedIn: signedIn)
            if !signedIn { lockWindow.tearDown() }
        }
        .onChange(of: scenePhase, initial: true) { _, phase in
            lock.sceneChanged(to: phase)
            Task { await lock.autoPrompt() }
        }
        .onChange(of: lock.isCovered, initial: true) { _, covered in
            if covered { lockWindow.show(lock) } else { lockWindow.hide() }
        }
    }
}
