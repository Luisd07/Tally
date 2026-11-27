import SwiftUI

/// When the user is debt-free at their planned payments and what interest costs, with a monthly budget to try
/// and the avalanche and snowball strategies for it side by side.
struct PayoffPlanView: View {
    @Environment(APIClient.self) private var api
    @State private var plan = OneShotLoad<PayoffPlan>()
    /// The budget asked for; nil for the planned payments.
    @State private var budget: Money?
    @State private var sliderStep = 0.0

    var body: some View {
        content
            .navigationTitle("Payoff plan")
            .navigationBarTitleDisplayMode(.inline)
            .task(id: budget) { await load() }
            .refreshable { await plan.load { try await api.payoffPlan(budget: budget) } }
    }

    @ViewBuilder
    private var content: some View {
        if let value = plan.value {
            PayoffPlanList(plan: value, error: plan.error, sliderStep: $sliderStep, budget: $budget) {
                Task { await load() }
            }
        } else if let error = plan.error {
            LoadFailedView(title: "Couldn't load the plan", error: error) { Task { await load() } }
        } else {
            ProgressView().frame(maxHeight: .infinity)
        }
    }

    /// Loads the plan for `budget`, a moment after the slider stops so a drag sends one request.
    private func load() async {
        if budget != nil {
            do { try await Task.sleep(for: .milliseconds(300)) } catch { return }
        }
        await plan.load { try await api.payoffPlan(budget: budget) }
    }
}

/// The loaded plan: the debt-free date and interest at the planned payments, the budget slider, both strategies
/// side by side and each card on its own.
struct PayoffPlanList: View {
    let plan: PayoffPlan
    var error: APIError?
    @Binding var sliderStep: Double
    @Binding var budget: Money?
    var retry: () -> Void = {}

    var body: some View {
        List {
            if let error {
                RetrySection(title: "Couldn't refresh", error: error, retry: retry)
            }
            Section("At your planned payments") {
                Text(plan.debtFreeMonth.debtFreeText).font(.headline)
                LabeledContent("Total interest", value: plan.totalInterest?.formatted() ?? "—")
                LabeledContent("Planned payments", value: "\(plan.plannedBudget.formatted())/mo")
            }
            if plan.cards.isEmpty {
                Section { Text("No card balances to pay off").foregroundStyle(.secondary) }
            } else {
                budgetSection
                Section("Where the extra goes") {
                    HStack(alignment: .top, spacing: 12) {
                        strategyColumn(plan.avalanche)
                        Divider()
                        strategyColumn(plan.snowball)
                    }
                }
                Section("Cards") {
                    ForEach(plan.cards) { card in cardRow(card) }
                }
            }
        }
    }

    private var budgetSection: some View {
        Section("Monthly budget") {
            LabeledContent("Pay each month", value: plan.budget(atStep: Int(sliderStep)).formatted())
            if let clamped = plan.clampedText {
                Text(clamped).font(.footnote).foregroundStyle(.orange)
            }
            if plan.budgetSteps > 0 {
                Slider(value: $sliderStep, in: 0...Double(plan.budgetSteps), step: 1) {
                    Text("Monthly budget")
                } minimumValueLabel: {
                    Text(plan.plannedBudget.formatted()).font(.caption)
                } maximumValueLabel: {
                    Text(plan.maxBudget.formatted()).font(.caption)
                }
                .onChange(of: sliderStep) { _, step in
                    let picked = plan.budget(atStep: Int(step))
                    budget = picked == plan.plannedBudget ? nil : picked
                }
            }
        }
    }

    private func strategyColumn(_ strategy: PayoffStrategy) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(strategy.title).font(.headline)
            Text(strategy.subtitle).font(.caption).foregroundStyle(.secondary)
            Text(strategy.debtFreeMonth.debtFreeText).font(.subheadline)
            Text("Interest \(strategy.totalInterest?.formatted() ?? "—")").font(.subheadline)
            if let saved = strategy.savedText {
                Text(saved).font(.subheadline).foregroundStyle(.green)
            }
            ForEach(Array(strategy.order.enumerated()), id: \.element.id) { index, step in
                Text("\(index + 1). \(step.name)").font(.footnote)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func cardRow(_ card: PayoffCard) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            LabeledContent(card.name, value: card.owed.formatted())
            Text(card.payoffText).font(.footnote).foregroundStyle(.secondary)
            if let promo = card.promo {
                Text(promo.payText).font(.footnote).foregroundStyle(promo.short ? .orange : .secondary)
            }
        }
    }
}
