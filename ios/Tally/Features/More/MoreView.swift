import SwiftUI

struct MoreView: View {
    @Environment(Session.self) private var session
    @Environment(APIClient.self) private var api
    @Environment(AppearanceStore.self) private var appearance
    /// Nil until loaded, and left nil when the request fails: the line is a nice-to-have.
    @State private var aiUsage: AIUsage?

    var body: some View {
        NavigationStack {
            List {
                if aiUsage?.enabled == true {
                    NavigationLink { ChatView() } label: {
                        Label("Ask about your money", systemImage: "sparkles")
                    }
                }
                NavigationLink("Dashboard") { DashboardView() }
                NavigationLink("Income") { IncomeView() }
                NavigationLink("Subscriptions") { SubscriptionsView() }
                NavigationLink("Cards") { CardsView() }
                NavigationLink("Rules") { RulesView() }
                NavigationLink("Categories") { CategoriesView() }
                NavigationLink("Import") { ImportView() }
                NavigationLink("Bank connection") { BankConnectionView() }
                NavigationLink("Reimbursements") { ReimbursementsView() }
                NavigationLink("Mileage") { MileageView() }
                NavigationLink("Tax") { TaxView() }
                UnsentCapturesSection()
                OutboxNoticesSection()
                if let aiUsage, aiUsage.enabled {
                    Section("AI assist") {
                        LabeledContent(
                            "This month",
                            value: "\(aiUsage.spentUsd.formatted()) of \(aiUsage.budgetUsd.formatted())"
                        )
                    }
                }
                Section("Display") {
                    @Bindable var appearance = appearance
                    Picker("Appearance", selection: $appearance.preference) {
                        ForEach(AppearancePreference.allCases) { Text($0.title).tag($0) }
                    }
                }
                Section("Account") {
                    LabeledContent("Server", value: session.serverURL.absoluteString)
                    NavigationLink("Sign-in activity") { SignInActivityView() }
                    Button("Sign Out", role: .destructive) { session.signOut() }
                }
            }
            .navigationTitle("More")
            .task { if let usage = try? await api.aiUsage() { aiUsage = usage } }
        }
    }
}
