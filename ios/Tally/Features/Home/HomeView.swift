import Charts
import SwiftUI

struct HomeView: View {
    @Environment(APIClient.self) private var api
    @Environment(\.theme) private var theme
    @State private var state = MonthValue<HomeSnapshot>()
    @State private var month = MonthRef()
    /// The newest AI weekly review (#173); nil before the first one, when AI is off, or when it failed to load.
    @State private var review: WeeklyReview?
    /// The insights worth a heads-up (#225); empty when there are none or they failed to load.
    @State private var headsUp = HeadsUpModel()
    @State private var showChat = HomeView.opensChatAtLaunch
    /// Called after an edit from Home saved, so the Inbox badge can catch up.
    var onEdited: (() -> Void)?
    /// Switches to the Inbox tab, for the Getting started checklist.
    var openInbox: (() -> Void)?
    @Environment(PushNotifications.self) private var push
    @State private var aiEnabled = false
    @State private var gettingStartedDismissed = GettingStartedPreferences().dismissed
    private let latestMonth = MonthRef()

    var body: some View {
        NavigationStack {
            MonthListScreen(
                title: "Home",
                failedTitle: "Couldn't load Home",
                month: $month,
                state: state,
                placement: .content,
                fetch: fetch
            ) { snapshot in
                gettingStartedSection(snapshot.home)
                headsUpSection
                staleBanksSection(snapshot.home)
                askSection
                if let glance = snapshot.glance {
                    MonthGlanceSections(glance: glance, month: $month, latest: latestMonth)
                    reviewSection
                } else {
                    summarySection(snapshot.home)
                    reviewSection
                    if theme.direction == .b {
                        categoriesSection(snapshot.categories)
                    } else {
                        spendingSection(snapshot.home)
                    }
                    incomeSection(snapshot.home)
                    owedSection(snapshot.home)
                    billsSection(snapshot.home)
                }
            }
            .themedList(theme)
            .toolbar(.hidden, for: .navigationBar)
            .task { await loadAI() }
            .navigationDestination(isPresented: $showChat) { chatDestination }
            .task { aiEnabled = (try? await api.aiUsage())?.enabled ?? false }
            .reloadOnReturnToForeground { await loadAI() }
            .onChange(of: push.registrations) {
                Task { await state.load(month: month) { try await fetch(month) } }
            }
        }
    }

    /// What to do next, until every step is done or the user dismisses it; on the current month only.
    @ViewBuilder
    private func gettingStartedSection(_ home: Home) -> some View {
        if month == latestMonth, let setup = home.setup,
           GettingStartedStep.isVisible(setup, dismissed: gettingStartedDismissed) {
            GettingStartedSection(
                setup: setup,
                aiEnabled: aiEnabled,
                openInbox: { openInbox?() },
                enableNotifications: { Task { await push.enableFromSetup() } },
                dismiss: {
                    GettingStartedPreferences().dismissed = true
                    gettingStartedDismissed = true
                }
            )
        }
    }

    /// A warning per bank that stopped refreshing, on the current month only.
    @ViewBuilder
    private func staleBanksSection(_ home: Home) -> some View {
        if month == latestMonth, let banks = home.staleBanks, !banks.isEmpty {
            Section {
                ForEach(banks, id: \.self) { StaleBankRow(bank: $0) }
            }
        }
    }

    /// Current insights, shown on the current month only.
    @ViewBuilder
    private var headsUpSection: some View {
        if month == latestMonth, !headsUp.isEmpty {
            HeadsUpSection(model: headsUp, onEdited: onEdited)
        }
    }

    /// Last week's review, shown on the current month only.
    @ViewBuilder
    private var reviewSection: some View {
        if month == latestMonth, let review {
            WeeklyReviewSection(review: review, onEdited: onEdited)
        }
    }

    private func loadAI() async {
        async let insights: Void = headsUp.load(api: api)
        await loadReview()
        await insights
    }

    @ViewBuilder
    private var askSection: some View {
        if aiEnabled {
            Section {
                EditRowButton { showChat = true } label: {
                    Label("Ask Tally", systemImage: "sparkles")
                    Spacer()
                }
            }
        }
    }

    @ViewBuilder
    private var chatDestination: some View {
        #if DEBUG
        ChatView(model: ProcessInfo.processInfo.environment["TALLY_DEV_OPEN_CHAT"] == "seed" ? .sample : ChatModel())
        #else
        ChatView()
        #endif
    }

    /// DEBUG only: `SIMCTL_CHILD_TALLY_DEV_OPEN_CHAT=empty|seed` opens the chat at launch, for screenshots.
    private static var opensChatAtLaunch: Bool {
        #if DEBUG
        ProcessInfo.processInfo.environment["TALLY_DEV_OPEN_CHAT"] != nil
        #else
        false
        #endif
    }

    private func loadReview() async {
        if let loaded = try? await api.weeklyReview() { review = loaded }
    }

    /// Home, plus the month's category spending when the theme shows budget bars, or everything the month at a
    /// glance needs in the mix direction.
    private func fetch(_ month: MonthRef) async throws -> HomeSnapshot {
        switch theme.direction {
        case .a:
            return HomeSnapshot(home: try await api.home(month: month))
        case .b:
            async let home = api.home(month: month)
            async let dashboard = api.dashboard(month: month, months: 1)
            return try await HomeSnapshot(home: home, categories: dashboard.byCategory)
        case .mix:
            return try await glanceSnapshot(month)
        }
    }

    private func glanceSnapshot(_ month: MonthRef) async throws -> HomeSnapshot {
        async let home = api.home(month: month)
        async let dashboard = api.dashboard(month: month, months: 1)
        async let inbox = api.inbox(limit: 1)
        async let business = api.missingReceipts(year: month.year, bucket: .business, limit: 1)
        async let reimbursable = api.missingReceipts(year: month.year, bucket: .reimbursable, limit: 1)
        async let subscriptions = api.subscriptions()
        let today = CalendarDate(date: .now)
        let loadedHome = try await home
        let glance = try await MonthGlance(
            home: loadedHome,
            dashboard: dashboard,
            toReview: inbox.total,
            missingReceipts: business.count + reimbursable.count,
            bills: MonthGlance.upcomingBills(
                subscriptions: subscriptions.items, homeBills: loadedHome.billsDue, month: month, today: today
            ),
            elapsed: MonthGlance.elapsed(in: month, today: today)
        )
        return HomeSnapshot(home: loadedHome, glance: glance)
    }

    private func summarySection(_ home: Home) -> some View {
        Section {
            VStack(alignment: .leading, spacing: 8) {
                PeriodSwitcher(month: $month, latest: latestMonth)
                summaryHeader(home)
            }
        }
        .listRowBackground(theme.direction == .a ? nil : Color.clear)
    }

    /// The month's spending; "Spent" alone, since the switcher above names the month.
    private func summaryHeader(_ home: Home) -> some View {
        SummaryHeader("Spent", amount: home.spendingByBucket.total) {
            switch theme.direction {
            case .a, .mix: SyncStatus(lastUpdated: home.lastUpdated)
            case .b: bucketLine(home)
            }
        } accessory: {
            switch theme.direction {
            case .a, .mix: SpendIncomeBar(spent: home.spendingByBucket.total, income: home.income.received)
            case .b: SyncStatus(lastUpdated: home.lastUpdated).font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    /// "Personal $166.10 · Business $225.00" under the hero.
    private func bucketLine(_ home: Home) -> some View {
        HStack(spacing: 4) {
            ForEach(Array(home.spendingByBucket.buckets.enumerated()), id: \.element.bucket) { index, item in
                if index > 0 { Text("·") }
                Text(item.bucket.label)
                AmountText(item.spent, role: .neutral)
            }
        }
    }

    private func spendingSection(_ home: Home) -> some View {
        Section("Spending") {
            ForEach(home.spendingByBucket.buckets, id: \.bucket) { item in
                LabeledContent(item.bucket.label) { AmountText(item.spent) }
            }
            NavigationLink("See breakdown") { DashboardView(month: month) }
        }
    }

    /// Each category's share of the month: of its budget when it has one, else of all spending.
    private func categoriesSection(_ categories: [CategorySpend]) -> some View {
        Section("Categories") {
            ForEach(categories) { category in
                CategoryBudgetRow(category: category, totalSpent: categories.reduce(Money(0)) { $0 + $1.spent }, month: month)
            }
            NavigationLink("See breakdown") { DashboardView(month: month) }
        }
    }

    private func incomeSection(_ home: Home) -> some View {
        let remaining = home.income.expected - home.income.received
        return Section("Income") {
            LabeledContent("Received") { AmountText(home.income.received, role: .flow) }
            LabeledContent("Expected") { AmountText(home.income.expected) }
            if remaining > Money(0) {
                LabeledContent("Still to come") { AmountText(remaining, role: .neutral) }
            }
        }
    }

    @ViewBuilder
    private func owedSection(_ home: Home) -> some View {
        if home.owedToYou > Money(0) {
            Section {
                NavigationLink { ReimbursementsView() } label: {
                    LabeledContent("Owed to you") { AmountText(home.owedToYou) }
                }
            }
        }
    }

    private func billsSection(_ home: Home) -> some View {
        Section("Bills due") {
            if home.billsDue.isEmpty {
                Label("Nothing due in the next 7 days", systemImage: "checkmark.circle")
                    .foregroundStyle(.secondary)
            }
            ForEach(home.billsDue) { bill in
                HStack(spacing: 12) {
                    CategoryGlyph(systemImage: bill.kind.symbol)
                    VStack(alignment: .leading) {
                        Text(bill.name)
                        Text(bill.dueDate.formatted()).font(theme.captionFont).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if let amount = bill.amount {
                        AmountText(amount)
                    } else {
                        Text("—").foregroundStyle(.secondary)
                    }
                }
            }
        }
    }
}

/// What Home shows for a month. `categories` is empty unless the theme draws budget bars; `glance` is set only in
/// the mix direction.
struct HomeSnapshot {
    let home: Home
    var categories: [CategorySpend] = []
    var glance: MonthGlance?
}

/// "Chase hasn't refreshed since Oct 24", with what to do about it.
struct StaleBankRow: View {
    let bank: StaleBank

    var body: some View {
        Label {
            VStack(alignment: .leading, spacing: 2) {
                Text("\(bank.institution) hasn't refreshed since \(bank.lastRefreshed.formatted())")
                Text("New charges won't show up. Sign in to SimpleFIN Bridge to reconnect it, then tap Sync now.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        } icon: {
            Image(systemName: "exclamationmark.arrow.circlepath").foregroundStyle(Color(.attention))
        }
    }
}

/// "Updated 2 days ago", with an `Attention` icon once the last sync is more than 36 hours old.
struct SyncStatus: View {
    let lastUpdated: Date?

    var body: some View {
        if let lastUpdated {
            let text = "Updated \(lastUpdated.formatted(.relative(presentation: .named)))"
            if Date.now.timeIntervalSince(lastUpdated) > 36 * 3600 {
                HStack(spacing: 4) {
                    Image(systemName: "exclamationmark.arrow.circlepath").foregroundStyle(Color(.attention))
                    Text(text).foregroundStyle(.secondary)
                }
            } else {
                Text(text)
            }
        } else {
            Text("Never synced")
        }
    }
}

/// Spending against income received this month, as two bars on one scale.
struct SpendIncomeBar: View {
    let spent: Money
    let income: Money

    var body: some View {
        Chart {
            BarMark(x: .value("Amount", spent.chartValue), y: .value("Kind", "Spent"))
                .foregroundStyle(Color.primary)
                .annotation(position: .trailing) { AmountText(spent).font(.caption) }
            BarMark(x: .value("Amount", income.chartValue), y: .value("Kind", "Income"))
                .foregroundStyle(Color(.moneyIn))
                .annotation(position: .trailing) { AmountText(income, role: .flow).font(.caption) }
        }
        .chartXAxis(.hidden)
        .chartXScale(domain: 0...max(spent.chartValue, income.chartValue, 1) * 1.3)
        .chartYAxis {
            AxisMarks { _ in AxisValueLabel().font(.caption) }
        }
        .frame(height: 56)
        .chartAccessibility(
            "Spent and income",
            summary: "Spent \(spent.formatted()), income received \(income.formatted())"
        )
    }
}

/// A category's spending with a bar: of its budget when it has one, else its share of the month's spending.
struct CategoryBudgetRow: View {
    @Environment(\.showCategoryTransactions) private var showTransactions
    let category: CategorySpend
    let totalSpent: Money
    let month: MonthRef

    var body: some View {
        if let categoryId = category.categoryId, let showTransactions {
            Button { showTransactions(TransactionFocus(categoryId: categoryId, month: month)) } label: {
                content.contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityHint("Shows its transactions")
        } else {
            content
        }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Text(category.displayName)
                Spacer()
                AmountText(category.spent)
            }
            ProgressView(value: min(fraction, 1))
                .tint(CategoryColor.barColor(for: category))
            Text(caption)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(category.accessibilitySummary)
    }

    private var fraction: Double {
        let whole = category.budget ?? totalSpent
        guard whole > Money(0) else { return 0 }
        return category.spent.chartValue / whole.chartValue
    }

    private var caption: String {
        if let remaining = category.remainingText { return remaining }
        return "\(Int((fraction * 100).rounded()))% of spending"
    }
}

private extension BillKind {
    var symbol: String {
        switch self {
        case .subscription: "arrow.triangle.2.circlepath"
        case .card: "creditcard"
        case .other: "doc.text"
        }
    }
}
