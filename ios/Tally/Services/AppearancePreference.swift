import Observation
import SwiftUI
import UIKit

enum AppearancePreference: String, CaseIterable, Identifiable {
    case system, light, dark

    var id: String { rawValue }

    var title: String {
        switch self {
        case .system: "System"
        case .light: "Light"
        case .dark: "Dark"
        }
    }

    /// Nil follows the system setting.
    var colorScheme: ColorScheme? {
        switch self {
        case .system: nil
        case .light: .light
        case .dark: .dark
        }
    }

    var userInterfaceStyle: UIUserInterfaceStyle {
        switch self {
        case .system: .unspecified
        case .light: .light
        case .dark: .dark
        }
    }

    /// Sets the style on every window. `.preferredColorScheme(nil)` alone can leave a forced
    /// Light or Dark in place after switching back to System.
    @MainActor
    func apply(to windows: [UIWindow]) {
        for window in windows { window.overrideUserInterfaceStyle = userInterfaceStyle }
    }

    @MainActor
    func applyToAppWindows() {
        let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
        apply(to: scenes.flatMap(\.windows))
    }
}

/// The chosen appearance, kept in UserDefaults. Not user-specific, so sign-out leaves it alone.
@MainActor
@Observable
final class AppearanceStore {
    static let shared = AppearanceStore()
    static let key = "appearance_preference"

    @ObservationIgnored private let defaults: UserDefaults

    var preference: AppearancePreference {
        didSet { defaults.set(preference.rawValue, forKey: Self.key) }
    }

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        preference = defaults.string(forKey: Self.key).flatMap(AppearancePreference.init) ?? .system
    }
}
