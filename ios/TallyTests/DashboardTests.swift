import Foundation
import SwiftUI
import Testing
@testable import Tally

/// Lives in `APIClientTests`' serialized suite because the network tests share `StubURLProtocol`'s one handler.
extension APIClientTests {
    private func dashboardJSON(month: String = "2026-10") -> Data {
        Data("""
        {"month":"\(month)","by_category":[{"category_id":null,"name":null,"budget":"50.00","spent":"60.00","remaining":"-10.00"}],\
        "total_spent":"60.00","total_budget":"50.00","trend":[{"month":"\(month)","spent":"60.00"}]}
        """.utf8)
    }

    @Test func dashboardFixtureDecodesToTheListedValues() throws {
        let dashboard = try JSONDecoder.api.decode(Dashboard.self, from: jsonFixture("dashboard-2026-10"))
        #expect(dashboard.month == MonthRef(year: 2026, month: 10))
        #expect(dashboard.byCategory.map(\.displayName) == ["Groceries", "Meals", "Travel"])
        #expect(dashboard.byCategory.map(\.spent) == [money("116.60"), money("14.50"), money("260.00")])
        #expect(dashboard.byCategory.allSatisfy { $0.budget == nil && !$0.isOverBudget })
        #expect(dashboard.totalSpent == money("391.10"))
        #expect(dashboard.totalBudget == money("0.00"))
        #expect(!dashboard.hasBudget)
        #expect(dashboard.trend.count == 6)
        #expect(dashboard.trend.first?.month == MonthRef(year: 2026, month: 5))
        #expect(dashboard.trend.last?.month == MonthRef(year: 2026, month: 10))
        #expect(dashboard.trend.last?.spent == money("391.10"))
    }

    @Test func uncategorizedRowIsNamedAndOverBudgetWhenRemainingIsNegative() throws {
        let dashboard = try JSONDecoder.api.decode(Dashboard.self, from: dashboardJSON())
        let row = try #require(dashboard.byCategory.first)
        #expect(row.displayName == "Uncategorized")
        #expect(row.isOverBudget)
        #expect(dashboard.hasBudget)
    }

    @Test func aBadTrendMonthFailsToDecode() {
        #expect(throws: DecodingError.self) {
            try JSONDecoder.api.decode(TrendMonth.self, from: Data(#"{"month":"2026-13","spent":"0.00"}"#.utf8))
        }
    }

    @Test func underBudgetRowShowsWhatIsLeft() {
        let row = CategorySpend(categoryId: UUID(), name: "Meals", budget: money("100.00"), spent: money("40.00"), remaining: money("60.00"))
        #expect(!row.isOverBudget)
        #expect(row.budgetText == "of \(money("100.00").formatted())")
        #expect(row.remainingText == "\(money("60.00").formatted()) left")
        #expect(row.accessibilitySummary.contains("left"))
    }

    @Test func overBudgetRowShowsTheExcess() {
        let row = CategorySpend(categoryId: UUID(), name: "Meals", budget: money("50.00"), spent: money("60.00"), remaining: money("-10.00"))
        #expect(row.remainingText == "over by \(money("10.00").formatted())")
        #expect(row.accessibilitySummary.contains("over by"))
    }

    @Test func rowWithoutABudgetHasNoBudgetText() {
        let row = CategorySpend(categoryId: UUID(), name: "Meals", budget: nil, spent: money("40.00"), remaining: nil)
        #expect(row.budgetText == nil && row.remainingText == nil)
    }

    @Test func aCategoryNamedUncategorizedStaysSeparateFromTheRollupRow() {
        let named = CategorySpend(categoryId: UUID(), name: "Uncategorized", budget: nil, spent: money("1.00"), remaining: nil)
        let rollup = CategorySpend(categoryId: nil, name: nil, budget: nil, spent: money("2.00"), remaining: nil)
        #expect(named.displayName == rollup.displayName)
        #expect(named.id != rollup.id)
    }

    @Test func remainingIsNegativeWhenSpendingPassesTheBudget() throws {
        let dashboard = try JSONDecoder.api.decode(Dashboard.self, from: dashboardJSON())
        #expect(dashboard.remaining == money("-10.00"))
    }

    @Test func dashboardEndpointSendsMonthAndOnlyANonDefaultMonthCount() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        var requests = reply(200, body: dashboardJSON()).makeAsyncIterator()

        _ = try await api.dashboard(month: MonthRef(year: 2026, month: 10))
        let first = try #require(await requests.next())
        #expect(first.url?.path() == "/api/dashboard")
        #expect(queryItems(first) == [URLQueryItem(name: "month", value: "2026-10")])

        _ = try await api.dashboard(month: MonthRef(year: 2026, month: 10), months: 12)
        let second = try #require(await requests.next())
        #expect(queryItems(second) == [URLQueryItem(name: "month", value: "2026-10"), URLQueryItem(name: "months", value: "12")])
    }

    @Test func dashboardModelIgnoresAStaleMonthResponse() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = MonthValue<Dashboard>()
        let (arrived, arrivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let october = dashboardJSON()
        let november = dashboardJSON(month: "2026-11")
        StubURLProtocol.respond { request in
            if request.url?.query()?.contains("2026-10") == true {
                arrivedSignal.yield()
                for await _ in release { break }
                return (StubURLProtocol.response(to: request, status: 200), october)
            }
            return (StubURLProtocol.response(to: request, status: 200), november)
        }

        let slow = Task { await model.load(month: MonthRef(year: 2026, month: 10)) { try await api.dashboard(month: MonthRef(year: 2026, month: 10)) } }
        for await _ in arrived { break }
        await model.load(month: MonthRef(year: 2026, month: 11)) { try await api.dashboard(month: MonthRef(year: 2026, month: 11)) }
        releaseSignal.yield()
        await slow.value

        #expect(model.value?.month == MonthRef(year: 2026, month: 11))
    }

    @Test func failedDashboardRefreshKeepsTheDashboardAndSetsError() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = MonthValue<Dashboard>()
        _ = reply(200, body: dashboardJSON())
        await model.load(month: MonthRef(year: 2026, month: 10)) { try await api.dashboard(month: MonthRef(year: 2026, month: 10)) }
        _ = reply(500, body: Data(#"{"error":{"code":"internal","message":"Boom.","request_id":"r2"}}"#.utf8))
        await model.load(month: MonthRef(year: 2026, month: 10)) { try await api.dashboard(month: MonthRef(year: 2026, month: 10)) }

        #expect(model.value?.totalSpent == money("60.00"))
        #expect(model.error?.errorDescription == "Boom.")
        #expect(model.hasLoaded)
        #expect(model.hasData)
    }
}

@MainActor
struct CategoryColorTests {
    private func spend(_ name: String?, remaining: String?) -> CategorySpend {
        CategorySpend(
            categoryId: name == nil ? nil : UUID(), name: name, budget: nil,
            spent: Money(Decimal(10)), remaining: remaining.flatMap { Decimal(string: $0) }.map { Money($0) }
        )
    }

    @Test func dashboardChartColorsMatchHomeBars() {
        let rows = [
            spend("Groceries", remaining: nil), spend("Gas", remaining: "5.00"),
            spend("Hobbies", remaining: "-3.00"), spend(nil, remaining: nil),
        ]
        let colors = CategoryChart.colors(for: rows)
        for (color, row) in zip(colors, rows) {
            #expect(color == CategoryColor.barColor(for: row), "\(row.displayName)")
        }
        #expect(colors[2] == Color.red, "over budget")
        #expect(colors[0] == CategoryColor.green, "groceries")
    }

    @Test func categoriesSharingANameKeepTheirOwnColors() {
        let rows = [spend("Gas", remaining: "5.00"), spend("Gas", remaining: "-3.00")]
        #expect(Set(rows.map(\.id)).count == 2)
        let colors = CategoryChart.colors(for: rows)
        #expect(colors[0] == CategoryColor.barColor(for: rows[0]))
        #expect(colors[1] == Color.red)
    }
}

@MainActor
struct CategoryFocusTests {
    @Test func showingACategoryReplacesTheFilter() {
        let id = UUID()
        let month = MonthRef(year: 2026, month: 9)
        var filter = TransactionFilter(search: "gas", bucket: .business, accountId: UUID())
        filter.show(TransactionFocus(categoryId: id, month: month))
        #expect(filter.month == month)
        #expect(filter.categoryId == id)
        #expect(filter.search.isEmpty)
        #expect(filter.bucket == nil && filter.accountId == nil)
        #expect(filter.hasRefinements)
        filter.clearRefinements()
        #expect(!filter.hasRefinements)
    }

    @Test func cutBackQuestionIsAStarter() {
        #expect(ChatModel.starterPrompts.contains("Where can I cut back this month?"))
    }

    @Test func cutBackQuestionNamesThePastMonthShown() {
        let now = MonthRef(year: 2026, month: 10)
        #expect(ChatModel.cutBackQuestion(for: now, now: now) == "Where can I cut back this month?")
        let past = MonthRef(year: 2026, month: 3)
        #expect(ChatModel.cutBackQuestion(for: past, now: now) == "Where could I have cut back in \(past.title())?")
        #expect(past.title().contains("2026"))
    }
}
