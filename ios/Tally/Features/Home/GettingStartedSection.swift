import SwiftUI

/// The first-run steps, in order. `isDone` reads the server's `setup` flags.
enum GettingStartedStep: CaseIterable {
    case bank, inbox, categories, budgets, notifications

    var title: String {
        switch self {
        case .bank: "Connect your bank"
        case .inbox: "Review your Inbox"
        case .categories: "Set up categories"
        case .budgets: "Set budgets"
        case .notifications: "Turn on notifications"
        }
    }

    var reason: String {
        switch self {
        case .bank: "New charges come in on their own every night"
        case .inbox: "Confirm what each charge was; Tally learns as you go"
        case .categories: "Group spending the way you think about it"
        case .budgets: "Get a heads-up before you overspend"
        case .notifications: "Hear about new charges and anything unusual"
        }
    }

    func isDone(_ setup: SetupProgress) -> Bool {
        switch self {
        case .bank: setup.bankConnected
        case .inbox: setup.inboxReviewed
        case .categories: setup.categoriesReady
        case .budgets: setup.budgetsSet
        case .notifications: setup.notificationsOn
        }
    }

    /// Shown while any step is open and the user hasn't dismissed it.
    static func isVisible(_ setup: SetupProgress, dismissed: Bool) -> Bool {
        !dismissed && allCases.contains { !$0.isDone(setup) }
    }
}

/// Whether the user hid the Getting started section. `Session.signOut` clears it.
struct GettingStartedPreferences {
    private static let dismissedKey = "getting_started_dismissed"

    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    var dismissed: Bool {
        get { defaults.bool(forKey: Self.dismissedKey) }
        nonmutating set { defaults.set(newValue, forKey: Self.dismissedKey) }
    }

    func clear() {
        defaults.removeObject(forKey: Self.dismissedKey)
    }
}

/// A checklist at the top of Home: what to do next, checking itself off as the server sees each step done.
struct GettingStartedSection: View {
    let setup: SetupProgress
    let aiEnabled: Bool
    let openInbox: () -> Void
    let enableNotifications: () -> Void
    let dismiss: () -> Void

    var body: some View {
        Section {
            ForEach(GettingStartedStep.allCases, id: \.self) { step in
                row(step)
                if step == .categories && aiEnabled {
                    NavigationLink { ChatView() } label: {
                        Label("Ask the assistant to set them up", systemImage: "sparkles")
                    }
                }
            }
            Button("Dismiss", action: dismiss).foregroundStyle(.secondary)
        } header: {
            Text("Getting started")
        }
    }

    @ViewBuilder
    private func row(_ step: GettingStartedStep) -> some View {
        switch step {
        case .bank:
            NavigationLink { BankConnectionView() } label: { label(step) }
        case .inbox:
            Button(action: openInbox) { label(step).contentShape(Rectangle()) }.buttonStyle(.plain)
        case .categories, .budgets:
            NavigationLink { CategoriesView() } label: { label(step) }
        case .notifications:
            Button(action: enableNotifications) { label(step).contentShape(Rectangle()) }.buttonStyle(.plain)
        }
    }

    private func label(_ step: GettingStartedStep) -> some View {
        let done = step.isDone(setup)
        return HStack(spacing: 12) {
            Image(systemName: done ? "checkmark.circle.fill" : "circle")
                .foregroundStyle(done ? Color.green : Color.secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(step.title).foregroundStyle(.primary)
                Text(step.reason).font(.caption).foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityValue(done ? "Done" : "Not done")
    }
}
