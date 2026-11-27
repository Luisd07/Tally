import SwiftUI

@main
struct TallyApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var api: APIClient
    @State private var session: Session
    @State private var outbox: Outbox
    @State private var lock: AppLock
    @State private var appearance = AppearanceStore.shared
    @State private var networkRecovery: NetworkRecoveryTrigger
    #if DEBUG
    private let theme = Theme(launchEnvironment: ProcessInfo.processInfo.environment)
    #else
    private let theme = Theme.standard
    #endif

    init() {
        ExportFile.sweep()
        // Builds before the no-cache session stored API responses in the shared cache's Cache.db.
        URLCache.shared.removeAllCachedResponses()
        let session = Session()
        _session = State(initialValue: session)
        let api = APIClient(credentials: session)
        let outbox = Outbox.makeDefault(api: api)
        api.onSuccess = { [weak outbox] in
            guard let outbox, !outbox.pending.isEmpty else { return }
            outbox.requestFlush()
        }
        _api = State(initialValue: api)
        PushNotifications.shared.configure(api: api)
        _outbox = State(initialValue: outbox)
        _lock = State(initialValue: AppLock.makeDefault())
        _networkRecovery = State(initialValue: NetworkRecoveryTrigger { [weak outbox] in
            Task { @MainActor in
                guard let outbox, !outbox.pending.isEmpty else { return }
                outbox.requestFlush()
            }
        })
    }

    var body: some Scene {
        WindowGroup {
            AppLockGate(lock: lock) {
                RootTabView()
            } signedOut: {
                SignInView()
            }
            .environment(api)
            .environment(session)
            .environment(outbox)
            .environment(\.theme, theme)
            .environment(PushNotifications.shared)
            .environment(appearance)
            .preferredColorScheme(appearance.preference.colorScheme)
            .tint(theme.accent.color)
            .task(id: session.isSignedIn) {
                if session.isSignedIn { await PushNotifications.shared.register() }
            }
            .onChange(of: session.isSignedIn) { _, signedIn in
                if !signedIn { PushNotifications.shared.unregister() }
            }
        }
    }
}
