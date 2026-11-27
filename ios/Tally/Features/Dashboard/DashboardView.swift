import Charts
import SwiftUI

struct DashboardView: View {
    @Environment(APIClient.self) private var api
    @State private var state = MonthValue<Dashboard>()
    @State private var month: MonthRef
    @State private var aiEnabled = false

    init(month: MonthRef = MonthRef()) {
        _month = State(initialValue: month)
    }

    var body: some View {
        MonthListScreen(
            title: "Dashboard",
            failedTitle: "Couldn't load the dashboard",
            month: $month,
            state: state,
            fetch: { try await api.dashboard(month: $0) }
        ) { dashboard in
            headerSection(dashboard)
            if dashboard.byCategory.isEmpty {
                Section { Text("No spending this month").foregroundStyle(.secondary) }
            } else {
                chartSection(dashboard)
                categoriesSection(dashboard)
                cutBackSection
            }
            trendSection(dashboard)
        }
        .task { aiEnabled = (try? await api.aiUsage())?.enabled ?? false }
    }

    private func headerSection(_ dashboard: Dashboard) -> some View {
        let isOver = dashboard.remaining < Money(0)
        return Section {
            LabeledContent("Spent", value: dashboard.totalSpent.formatted()).fontWeight(.semibold)
            if dashboard.hasBudget {
                LabeledContent("Budget", value: dashboard.totalBudget.formatted())
                LabeledContent(isOver ? "Over by" : "Remaining", value: (isOver ? dashboard.remaining.negated : dashboard.remaining).formatted())
                    .foregroundStyle(isOver ? Color.red : Color.primary)
            }
        }
    }

    private func chartSection(_ dashboard: Dashboard) -> some View {
        Section("By category") {
            CategoryChart(categories: dashboard.byCategory)
        }
    }

    private func categoriesSection(_ dashboard: Dashboard) -> some View {
        Section {
            ForEach(dashboard.byCategory) { CategorySpendRow(category: $0) }
        }
    }

    @ViewBuilder
    private var cutBackSection: some View {
        if aiEnabled {
            Section {
                NavigationLink { ChatView(question: ChatModel.cutBackQuestion(for: month)) } label: {
                    Label("Ask Tally where to cut back", systemImage: "sparkles")
                }
            }
        }
    }

    private func trendSection(_ dashboard: Dashboard) -> some View {
        Section("Trend") {
            TrendChart(trend: dashboard.trend, selected: month)
        }
    }
}

private struct CategorySpendRow: View {
    let category: CategorySpend

    var body: some View {
        LabeledContent {
            TrailingDetail(value: category.spent.formatted()) { detail }
        } label: {
            Text(category.displayName)
        }
    }

    @ViewBuilder
    private var detail: some View {
        if let budgetText = category.budgetText, let remainingText = category.remainingText {
            Text(budgetText).font(.caption).foregroundStyle(.secondary)
            Text(remainingText).font(.caption).foregroundStyle(category.isOverBudget ? Color.red : Color.secondary)
        }
    }
}

struct CategoryChart: View {
    let categories: [CategorySpend]

    private static let rowHeight: CGFloat = 36
    private static let axisAllowance: CGFloat = 24

    var body: some View {
        Chart {
            ForEach(categories) { category in
                BarMark(x: .value("Spent", category.spent.chartValue), y: .value("Category", category.id))
                    .foregroundStyle(by: .value("Category", category.id))
                if let budget = category.budget {
                    PointMark(x: .value("Budget", budget.chartValue), y: .value("Category", category.id))
                        .symbol(.diamond)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .chartForegroundStyleScale(domain: categories.map(\.id), range: Self.colors(for: categories))
        .chartLegend(.hidden)
        .chartYAxis {
            AxisMarks { value in
                AxisValueLabel {
                    if let id = value.as(String.self) { Text(names[id] ?? "") }
                }
            }
        }
        .frame(height: CGFloat(categories.count) * Self.rowHeight + Self.axisAllowance)
        .chartAccessibility("Spending by category", summary: categories.map(\.accessibilitySummary).joined(separator: "; "))
    }

    /// The mark colors in category order: the same ones Home's bars use.
    static func colors(for categories: [CategorySpend]) -> [Color] {
        categories.map(CategoryColor.barColor(for:))
    }

    private var names: [String: String] {
        Dictionary(categories.map { ($0.id, $0.displayName) }) { first, _ in first }
    }
}

private struct TrendChart: View {
    let trend: [TrendMonth]
    let selected: MonthRef

    private static let height: CGFloat = 160

    var body: some View {
        Chart(trend) { item in
            BarMark(x: .value("Month", item.month.firstDay() ?? .distantPast, unit: .month), y: .value("Spent", item.spent.chartValue))
                .foregroundStyle(item.month == selected ? Color.accentColor : Color.secondary.opacity(0.4))
        }
        .chartXAxis { AxisMarks(values: .stride(by: .month)) { _ in AxisValueLabel(format: .dateTime.month(.narrow)) } }
        .frame(height: Self.height)
        .chartAccessibility("Spending by month", summary: trend.map { "\($0.month.title()) \($0.spent.formatted())" }.joined(separator: ", "))
    }
}
