import SwiftUI
import Testing
@testable import Tally

@MainActor
private final class FakeAuthenticator: Authenticator {
    var result: AuthResult
    private(set) var calls = 0
    private var gate: CheckedContinuation<Void, Never>?
    var blocks = false

    init(_ result: AuthResult = .success) { self.result = result }

    func authenticate() async -> AuthResult {
        calls += 1
        if blocks { await withCheckedContinuation { gate = $0 } }
        return result
    }

    func release() {
        gate?.resume()
        gate = nil
    }
}

@MainActor
struct AppLockTests {
    private func makeLock(_ auth: FakeAuthenticator, allowsWithoutPasscode: Bool = false) -> AppLock {
        AppLock(authenticator: auth, allowsWithoutPasscode: allowsWithoutPasscode)
    }

    /// Yields until `unlock()` reaches the authenticator; fails instead of hanging if it never does.
    private func waitForPrompt(_ auth: FakeAuthenticator) async throws {
        var tries = 0
        while auth.calls == 0, tries < 1_000 {
            tries += 1
            await Task.yield()
        }
        try #require(auth.calls > 0, "unlock() never reached the authenticator")
    }

    @Test func startsLocked() {
        let lock = makeLock(FakeAuthenticator())
        #expect(lock.isLocked)
        #expect(lock.isCovered)
        #expect(!lock.hasUnlocked)
    }

    @Test func successUnlocks() async {
        let lock = makeLock(FakeAuthenticator(.success))
        await lock.unlock()
        #expect(!lock.isLocked)
        #expect(lock.message == nil)
        #expect(lock.hasUnlocked)
    }

    @Test func userCancelStaysLockedWithoutMessage() async {
        let lock = makeLock(FakeAuthenticator(.cancelled))
        await lock.unlock()
        #expect(lock.isLocked)
        #expect(lock.message == nil)
        #expect(!lock.hasUnlocked)
    }

    @Test func failureStaysLockedWithMessage() async {
        let lock = makeLock(FakeAuthenticator(.failed("Too many attempts")))
        await lock.unlock()
        #expect(lock.isLocked)
        #expect(lock.message == .failed("Too many attempts"))
    }

    @Test func backgroundRelocksAndKeepsContentMounted() async {
        let lock = makeLock(FakeAuthenticator())
        await lock.unlock()
        lock.sceneChanged(to: .background)
        #expect(lock.isLocked)
        #expect(lock.isCovered)
        #expect(lock.hasUnlocked)
    }

    @Test func inactiveCoversWithoutLocking() async {
        let lock = makeLock(FakeAuthenticator())
        await lock.unlock()
        lock.sceneChanged(to: .inactive)
        #expect(!lock.isLocked)
        #expect(lock.isCovered)
        lock.sceneChanged(to: .active)
        #expect(!lock.isCovered)
    }

    @Test func inactiveRoundTripDoesNotReprompt() async {
        let auth = FakeAuthenticator(.cancelled)
        let lock = makeLock(auth)
        lock.sceneChanged(to: .active)
        await lock.autoPrompt()
        lock.sceneChanged(to: .inactive)
        await lock.autoPrompt()
        lock.sceneChanged(to: .active)
        await lock.autoPrompt()
        #expect(auth.calls == 1)
    }

    @Test func backgroundRoundTripReprompts() async {
        let auth = FakeAuthenticator(.cancelled)
        let lock = makeLock(auth)
        lock.sceneChanged(to: .active)
        await lock.autoPrompt()
        lock.sceneChanged(to: .background)
        lock.sceneChanged(to: .active)
        await lock.autoPrompt()
        #expect(auth.calls == 2)
    }

    @Test func activeWhileUnlockedDoesNotPrompt() async {
        let auth = FakeAuthenticator()
        let lock = makeLock(auth)
        await lock.unlock()
        lock.sceneChanged(to: .active)
        await lock.autoPrompt()
        #expect(auth.calls == 1)
    }

    @Test func secondUnlockDuringPromptIsIgnored() async throws {
        let auth = FakeAuthenticator()
        auth.blocks = true
        let lock = makeLock(auth)
        let first = Task { await lock.unlock() }
        try await waitForPrompt(auth)
        await lock.unlock()
        #expect(auth.calls == 1)
        auth.release()
        await first.value
        #expect(!lock.isLocked)
    }

    @Test func successAfterBackgroundStaysLocked() async throws {
        let auth = FakeAuthenticator(.success)
        auth.blocks = true
        let lock = makeLock(auth)
        let first = Task { await lock.unlock() }
        try await waitForPrompt(auth)
        lock.sceneChanged(to: .background)
        auth.release()
        await first.value
        #expect(lock.isLocked)
    }

    @Test func noPasscodeStaysLockedWhenNotAllowed() async {
        let lock = makeLock(FakeAuthenticator(.passcodeNotSet), allowsWithoutPasscode: false)
        await lock.unlock()
        #expect(lock.isLocked)
        #expect(lock.message == .passcodeRequired)
    }

    @Test func noPasscodeUnlocksWhenAllowed() async {
        let lock = makeLock(FakeAuthenticator(.passcodeNotSet), allowsWithoutPasscode: true)
        await lock.unlock()
        #expect(!lock.isLocked)
    }

    @Test func signOutDropsLockAndMessage() async {
        let lock = makeLock(FakeAuthenticator(.failed("x")))
        await lock.unlock()
        lock.sessionChanged(signedIn: false)
        #expect(!lock.isLocked)
        #expect(lock.message == nil)
        #expect(!lock.isCovered)
    }

    @Test func signedOutBackgroundThenSignInStartsUnlocked() {
        let auth = FakeAuthenticator()
        let lock = makeLock(auth)
        lock.sessionChanged(signedIn: false)
        lock.sceneChanged(to: .inactive)
        #expect(!lock.isCovered)
        lock.sceneChanged(to: .background)
        lock.sessionChanged(signedIn: true)
        lock.sceneChanged(to: .active)
        #expect(!lock.isLocked)
        #expect(lock.hasUnlocked)
        #expect(auth.calls == 0)
    }
}
