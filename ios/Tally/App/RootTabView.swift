import SwiftUI

struct RootTabView: View {
    @Environment(APIClient.self) private var api
    @Environment(Outbox.self) private var outbox
    @Environment(PushNotifications.self) private var push
    @State private var inbox = InboxModel()
    @State private var selection = Self.launchTab
    @State private var transactionsFocus: TransactionFocus?

    private enum Tab: String, Hashable {
        case home, inbox, add, transactions, more
    }

    /// DEBUG only: `SIMCTL_CHILD_TALLY_DEV_TAB=inbox|add|transactions|more` opens on that tab, for screenshots.
    private static var launchTab: Tab {
        #if DEBUG
        ProcessInfo.processInfo.environment["TALLY_DEV_TAB"].flatMap(Tab.init(rawValue:)) ?? .home
        #else
        .home
        #endif
    }

    var body: some View {
        TabView(selection: $selection) {
            HomeView(onEdited: { Task { await inbox.load(api: api) } }, openInbox: { selection = .inbox })
                .tabItem { Label("Home", systemImage: "house") }
                .tag(Tab.home)
            InboxView(model: inbox)
                .tabItem { Label("Inbox", systemImage: "tray") }
                .badge(inbox.total)
                .tag(Tab.inbox)
            AddView()
                .tabItem { Label("Add", systemImage: "plus.circle") }
                .tag(Tab.add)
            TransactionsView(
                isSelected: selection == .transactions,
                focus: $transactionsFocus,
                onEdited: { Task { await inbox.load(api: api) } }
            )
                .tabItem { Label("Transactions", systemImage: "list.bullet") }
                .tag(Tab.transactions)
            MoreView()
                .tabItem { Label("More", systemImage: "ellipsis") }
                .tag(Tab.more)
        }
        .environment(\.inboxEdited, { Task { await inbox.load(api: api) } })
        .environment(\.showCategoryTransactions, { focus in
            transactionsFocus = focus
            selection = .transactions
        })
        .onChange(of: push.opensInbox, initial: true) { _, opens in
            guard opens else { return }
            push.opensInbox = false
            selection = .inbox
        }
        .task {
            outbox.requestFlush()
            await inbox.load(api: api)
        }
        .reloadOnReturnToForeground {
            outbox.requestFlush()
            await inbox.load(api: api)
        }
    }
}
