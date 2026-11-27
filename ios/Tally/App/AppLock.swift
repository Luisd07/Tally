import Observation
import SwiftUI

enum LockMessage: Equatable {
    case passcodeRequired
    case failed(String)
}

/// Locks the signed-in app at launch and on every move to the background. Signed out, nothing is locked.
@MainActor
@Observable
final class AppLock {
    enum LockState { case locked, unlocked }

    private(set) var state = LockState.locked
    private(set) var message: LockMessage?
    /// False until the first unlock, so signed-in content isn't mounted (or loaded) before authenticating.
    private(set) var hasUnlocked = false
    private var phase = ScenePhase.active
    private var isSignedIn = true

    @ObservationIgnored private let authenticator: any Authenticator
    @ObservationIgnored private let allowsWithoutPasscode: Bool
    @ObservationIgnored private var isPrompting = false
    @ObservationIgnored private var autoPrompted = false
    /// Bumped whenever the lock is re-armed or dropped, so a prompt result from an earlier episode is discarded.
    @ObservationIgnored private var episode = 0

    init(authenticator: any Authenticator, allowsWithoutPasscode: Bool) {
        self.authenticator = authenticator
        self.allowsWithoutPasscode = allowsWithoutPasscode
    }

    /// DEBUG unlocks without a passcode so the simulator stays usable, and never prompts under XCTest;
    /// Release always prompts and stays locked without a passcode.
    static func makeDefault() -> AppLock {
        #if DEBUG
        struct UnlockedAuthenticator: Authenticator {
            func authenticate() async -> AuthResult { .success }
        }
        let underTest = ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil
        return AppLock(
            authenticator: underTest ? UnlockedAuthenticator() : LiveAuthenticator(),
            allowsWithoutPasscode: true
        )
        #else
        return AppLock(authenticator: LiveAuthenticator(), allowsWithoutPasscode: false)
        #endif
    }

    var isLocked: Bool { state == .locked }
    /// The lock screen while locked, or a plain privacy cover while inactive.
    var isCovered: Bool { isLocked || (isSignedIn && phase == .inactive) }

    /// Signing out drops the lock, so the next sign-in starts unlocked: the user just used a passkey.
    func sessionChanged(signedIn: Bool) {
        isSignedIn = signedIn
        guard !signedIn else { return }
        episode += 1
        autoPrompted = false
        setUnlocked()
    }

    func sceneChanged(to newPhase: ScenePhase) {
        phase = newPhase
        guard newPhase == .background, isSignedIn else { return }
        episode += 1
        state = .locked
        autoPrompted = false
    }

    /// Prompts once per lock episode, so the prompt's own inactive/active bounce after a cancel can't loop.
    func autoPrompt() async {
        guard phase == .active, isLocked, !autoPrompted else { return }
        autoPrompted = true
        await unlock()
    }

    func unlock() async {
        guard isLocked, !isPrompting else { return }
        isPrompting = true
        defer { isPrompting = false }
        let started = episode
        let result = await authenticator.authenticate()
        guard started == episode else { return }
        switch result {
        case .success: setUnlocked()
        case .cancelled: break
        case .passcodeNotSet where allowsWithoutPasscode: setUnlocked()
        case .passcodeNotSet: message = .passcodeRequired
        case .failed(let text): message = .failed(text)
        }
    }

    private func setUnlocked() {
        state = .unlocked
        message = nil
        hasUnlocked = true
    }
}
