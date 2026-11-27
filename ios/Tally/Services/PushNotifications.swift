import Foundation
import Observation
import os
import UIKit
import UserNotifications

/// Push notifications (#55): the server sends one summary after each sync that found new transactions.
///
/// While signed in, the app asks for permission and registers with APNs on every launch, then sends the token
/// to `POST /api/devices` (which also refreshes its last-seen time). Signing out unregisters, so APNs stops
/// accepting the old token and the server prunes it. Tapping a notification opens the Inbox.
@Observable
final class PushNotifications {
    static let shared = PushNotifications()

    private static let logger = Logger(subsystem: "com.luisd07.tally", category: "Push")

    /// Set when a notification is tapped; the tab bar switches to the Inbox and clears it.
    var opensInbox = false

    /// Counts tokens the server has accepted, so a screen that shows the server's view can reload.
    private(set) var registrations = 0

    @ObservationIgnored private var api: APIClient?
    @ObservationIgnored private let environment: PushEnvironment

    init(environment: PushEnvironment = .current) {
        self.environment = environment
    }

    func configure(api: APIClient) {
        self.api = api
    }

    /// Asks for permission (iOS asks only once) and, if allowed, registers with APNs. The token arrives in
    /// `didRegister(deviceToken:)`.
    func register() async {
        let center = UNUserNotificationCenter.current()
        do {
            guard try await center.requestAuthorization(options: [.alert, .sound, .badge]) else { return }
        } catch {
            Self.logger.error("Notification permission failed: \(error.localizedDescription, privacy: .public)")
            return
        }
        UIApplication.shared.registerForRemoteNotifications()
    }

    /// What the Getting started notifications step does for a permission state: iOS shows no prompt after a
    /// denial, so that case sends the user to Settings.
    nonisolated static func opensSettings(for status: UNAuthorizationStatus) -> Bool {
        status == .denied
    }

    /// The Getting started step: Settings when permission was denied, otherwise ask (a no-op prompt-wise if already
    /// allowed) and register, which re-sends the token.
    func enableFromSetup(
        status: UNAuthorizationStatus? = nil, openSettings: @MainActor () -> Void = PushNotifications.openAppSettings
    ) async {
        let current: UNAuthorizationStatus
        if let status {
            current = status
        } else {
            current = await UNUserNotificationCenter.current().notificationSettings().authorizationStatus
        }
        if Self.opensSettings(for: current) {
            openSettings()
        } else {
            await register()
        }
    }

    static func openAppSettings() {
        guard let url = URL(string: UIApplication.openSettingsURLString) else { return }
        UIApplication.shared.open(url)
    }

    func unregister() {
        UIApplication.shared.unregisterForRemoteNotifications()
    }

    /// Sends the APNs token to the server. A failure is logged and retried on the next launch.
    func didRegister(deviceToken: Data) async {
        guard let api else { return }
        do {
            _ = try await api.registerDevice(Self.registration(token: deviceToken, environment: environment))
            registrations += 1
        } catch {
            Self.logger.error("Device registration failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    func notificationTapped() {
        opensInbox = true
    }

    /// The token as the lowercase hex the server stores.
    nonisolated static func registration(token: Data, environment: PushEnvironment) -> DeviceRegistration {
        DeviceRegistration(apnsToken: token.map { String(format: "%02x", $0) }.joined(), environment: environment)
    }
}

extension PushEnvironment {
    /// From the `aps-environment` entitlement in the app's provisioning profile. App Store builds carry no
    /// profile and always use production.
    nonisolated static var current: PushEnvironment {
        let url = Bundle.main.url(forResource: "embedded", withExtension: "mobileprovision")
        let profile = url.flatMap { try? Data(contentsOf: $0) }
        #if targetEnvironment(simulator)
        return profile.map(from(provisioningProfile:)) ?? .sandbox
        #else
        return profile.map(from(provisioningProfile:)) ?? .production
        #endif
    }

    /// A provisioning profile is a signed (CMS) wrapper around a plain XML plist; this reads the plist's
    /// `Entitlements.aps-environment`. Anything unreadable counts as production.
    nonisolated static func from(provisioningProfile data: Data) -> PushEnvironment {
        guard let start = data.range(of: Data("<?xml".utf8)),
              let end = data.range(of: Data("</plist>".utf8), in: start.lowerBound..<data.endIndex),
              let plist = try? PropertyListSerialization.propertyList(
                  from: data[start.lowerBound..<end.upperBound], format: nil
              ) as? [String: Any],
              let entitlements = plist["Entitlements"] as? [String: Any],
              entitlements["aps-environment"] as? String == "development"
        else { return .production }
        return .sandbox
    }
}

/// Receives the APNs token and notification taps, which reach only the app delegate.
final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(
        _ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        // Set before launch finishes, so a tap that launched the app is delivered.
        UNUserNotificationCenter.current().delegate = self
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        Task { await PushNotifications.shared.didRegister(deviceToken: deviceToken) }
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: any Error) {
        Logger(subsystem: "com.luisd07.tally", category: "Push")
            .error("APNs registration failed: \(error.localizedDescription, privacy: .public)")
    }

    /// Shows the banner while Tally is open, too. The completion-handler forms are used on purpose: UIKit runs the
    /// completion of the async forms off the main thread, and `didReceive`'s asserts "Call must be made on main thread".
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter, willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping @Sendable (UNNotificationPresentationOptions) -> Void
    ) {
        DispatchQueue.main.async { completionHandler([.banner, .list, .sound]) }
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping @Sendable () -> Void
    ) {
        Self.handleTap(completion: completionHandler)
    }

    /// Opens the Inbox, then calls `completion`, both on the main thread.
    nonisolated static func handleTap(on push: PushNotifications? = nil, completion: @escaping @Sendable () -> Void) {
        DispatchQueue.main.async {
            MainActor.assumeIsolated { (push ?? .shared).notificationTapped() }
            completion()
        }
    }
}
