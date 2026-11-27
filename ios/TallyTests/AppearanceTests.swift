import SwiftUI
import Testing
import UIKit
@testable import Tally

@MainActor
struct AppearanceTests {
    private func defaults() -> UserDefaults {
        let suite = "AppearanceTests-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        return defaults
    }

    @Test func defaultsToSystem() {
        #expect(AppearanceStore(defaults: defaults()).preference == .system)
    }

    @Test func persistsAcrossInstances() {
        let defaults = defaults()
        AppearanceStore(defaults: defaults).preference = .dark
        #expect(AppearanceStore(defaults: defaults).preference == .dark)
    }

    @Test func mapsToColorSchemeAndInterfaceStyle() {
        #expect(AppearancePreference.system.colorScheme == nil)
        #expect(AppearancePreference.light.colorScheme == .light)
        #expect(AppearancePreference.dark.colorScheme == .dark)
        #expect(AppearancePreference.system.userInterfaceStyle == .unspecified)
        #expect(AppearancePreference.light.userInterfaceStyle == .light)
        #expect(AppearancePreference.dark.userInterfaceStyle == .dark)
    }

    @Test func applySetsTheStyleOnEveryWindow() {
        let windows = [UIWindow(), UIWindow()]
        AppearancePreference.dark.apply(to: windows)
        #expect(windows.allSatisfy { $0.overrideUserInterfaceStyle == .dark })
        AppearancePreference.system.apply(to: windows)
        #expect(windows.allSatisfy { $0.overrideUserInterfaceStyle == .unspecified })
    }

    @Test func signOutKeepsTheAppearance() throws {
        let fixture = TestSession()
        defer { try? fixture.cleanUp() }
        let store = AppearanceStore(defaults: fixture.defaults)
        store.preference = .light
        fixture.make().signOut()
        #expect(AppearanceStore(defaults: fixture.defaults).preference == .light)
    }
}
