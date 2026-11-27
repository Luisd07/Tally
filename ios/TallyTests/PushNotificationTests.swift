import Foundation
import Testing
import UserNotifications
@testable import Tally

struct PushEnvironmentTests {
    /// A profile-shaped blob: binary signature bytes around the XML plist, as in `embedded.mobileprovision`.
    private func profile(entitlements: String) -> Data {
        let plist = """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyLists-1.0.dtd">
        <plist version="1.0"><dict><key>Name</key><string>Tally</string>
        <key>Entitlements</key><dict>\(entitlements)</dict></dict></plist>
        """
        return Data([0x30, 0x82, 0x1F, 0x00, 0xFF]) + Data(plist.utf8) + Data([0xA0, 0x82, 0x00])
    }

    @Test func developmentProfileIsSandbox() {
        let data = profile(entitlements: "<key>aps-environment</key><string>development</string>")
        #expect(PushEnvironment.from(provisioningProfile: data) == .sandbox)
    }

    @Test func productionProfileIsProduction() {
        let data = profile(entitlements: "<key>aps-environment</key><string>production</string>")
        #expect(PushEnvironment.from(provisioningProfile: data) == .production)
    }

    @Test func profileWithoutPushOrUnreadableIsProduction() {
        #expect(PushEnvironment.from(provisioningProfile: profile(entitlements: "")) == .production)
        #expect(PushEnvironment.from(provisioningProfile: Data("not a profile".utf8)) == .production)
    }

    @Test func tokenIsLowercaseHex() {
        let registration = PushNotifications.registration(token: Data([0x0A, 0xFF, 0x00, 0x7B]), environment: .sandbox)
        #expect(registration.apnsToken == "0aff007b")
        #expect(registration.environment == .sandbox)
    }

    @MainActor @Test func tappingANotificationAsksForTheInbox() {
        let push = PushNotifications(environment: .sandbox)
        #expect(!push.opensInbox)
        push.notificationTapped()
        #expect(push.opensInbox)
    }

    @MainActor @Test func tapCompletionRunsOnTheMainThreadAndOpensTheInbox() async {
        let push = PushNotifications(environment: .sandbox)
        let onMain = await withCheckedContinuation { continuation in
            DispatchQueue.global().async {
                AppDelegate.handleTap(on: push) { continuation.resume(returning: Thread.isMainThread) }
            }
        }
        #expect(onMain)
        #expect(push.opensInbox)
    }

    /// Calls `didReceive` through its Objective-C selector, as UIKit does, from a background queue.
    @MainActor @Test func didReceiveDelegateCallbackCompletesOnTheMainThread() async {
        nonisolated(unsafe) let delegate = AppDelegate()
        let selector = NSSelectorFromString("userNotificationCenter:didReceiveNotificationResponse:withCompletionHandler:")
        let imp = delegate.method(for: selector)
        typealias DidReceive = @convention(c) (
            AnyObject, Selector, UNUserNotificationCenter, UNNotificationResponse?, @escaping @convention(block) () -> Void
        ) -> Void
        let call = unsafeBitCast(imp, to: DidReceive.self)
        let center = UNUserNotificationCenter.current()
        let onMain = await withCheckedContinuation { continuation in
            DispatchQueue.global().async {
                call(delegate, selector, center, nil) { continuation.resume(returning: Thread.isMainThread) }
            }
        }
        #expect(onMain)
    }
}

// MARK: - Network (shares StubURLProtocol, so these run in the serialized suite)

extension APIClientTests {
    @Test func deviceTokenIsRegisteredWithItsEnvironment() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let reply = #"{"id":"6f1c2b9e-0a4d-4e5f-9a8b-1c2d3e4f5a6b","apns_token":"0aff","environment":"sandbox","last_seen_at":"2026-10-24T00:00:00Z","created_at":"2026-10-24T00:00:00Z","updated_at":"2026-10-24T00:00:00Z"}"#
        let requests = self.reply(201, body: Data(reply.utf8))
        let push = PushNotifications(environment: .sandbox)
        push.configure(api: makeClient(session))

        await push.didRegister(deviceToken: Data([0x0A, 0xFF]))

        var iterator = requests.makeAsyncIterator()
        let request = try #require(await iterator.next())
        #expect(request.httpMethod == "POST")
        #expect(request.url?.path() == "/api/devices")
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer t1")
        let body = try #require(bodyData(request))
        let json = try #require(JSONSerialization.jsonObject(with: body) as? [String: String])
        #expect(json == ["apns_token": "0aff", "environment": "sandbox"])
    }
}

struct NotificationStepTests {
    @Test func onlyADeniedPermissionOpensSettings() {
        #expect(PushNotifications.opensSettings(for: .denied))
        for status in [UNAuthorizationStatus.notDetermined, .authorized, .provisional] {
            #expect(!PushNotifications.opensSettings(for: status))
        }
    }

    @MainActor @Test func deniedOpensSettingsInsteadOfRegistering() async {
        var opened = false
        await PushNotifications(environment: .sandbox).enableFromSetup(status: .denied) { opened = true }
        #expect(opened)
    }

    @MainActor @Test func registrationsCountOnlyAcceptedTokens() async {
        let push = PushNotifications(environment: .sandbox)
        await push.didRegister(deviceToken: Data([1]))
        #expect(push.registrations == 0)
    }
}
