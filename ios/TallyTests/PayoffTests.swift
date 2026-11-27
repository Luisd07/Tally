import Foundation
import Testing
@testable import Tally

/// Lives in `APIClientTests`' serialized suite because the network tests share `StubURLProtocol`'s one handler.
extension APIClientTests {
    private func payoffPlan() throws -> PayoffPlan {
        try JSONDecoder.api.decode(PayoffPlan.self, from: jsonFixture("payoff"))
    }

    // MARK: Models

    @Test func payoffFixtureDecodesToTheListedValues() throws {
        let plan = try payoffPlan()
        #expect(plan.budget == money("400.00"))
        #expect(plan.requestedBudget == money("400.00"))
        #expect(plan.clamped == false)
        #expect(plan.clampedText == nil)
        #expect(plan.plannedBudget == money("250.00"))
        #expect(plan.monthsToDebtFree == 48)
        #expect(plan.debtFreeMonth == MonthRef(year: 2030, month: 10))
        #expect(plan.totalInterest == money("2007.12"))
        #expect(plan.cards.map(\.name) == ["Sapphire", "Freedom", "Slate"])
        let slate = plan.cards[2]
        #expect(slate.apr == "21.00")
        #expect(slate.promoAprEnd == CalendarDate(year: 2027, month: 4, day: 1))
        #expect(slate.plannedPayment == money("100.00"))
        #expect(slate.payoffMonth == MonthRef(year: 2027, month: 11))
        #expect(slate.promo == PromoPace(
            ends: CalendarDate(year: 2027, month: 4, day: 1), monthsLeft: 5, monthlyPayment: money("240.00"), short: true
        ))
        #expect(plan.cards[0].promo == nil)
        #expect(plan.avalanche.strategy == .avalanche)
        #expect(plan.avalanche.order.map(\.name) == ["Sapphire", "Freedom", "Slate"])
        #expect(plan.avalanche.debtFreeMonth == MonthRef(year: 2028, month: 1))
        #expect(plan.avalanche.interestSaved == money("1303.02"))
        #expect(plan.snowball.strategy == .snowball)
        #expect(plan.snowball.order.map(\.name) == ["Freedom", "Slate", "Sapphire"])
        #expect(plan.snowball.totalInterest == money("762.08"))
    }

    @Test func payoffNeverDecodesToNil() throws {
        let json = """
        {"budget":"100.00","requested_budget":"40.00","clamped":true,"planned_budget":"100.00",\
        "months_to_debt_free":null,"debt_free_month":null,\
        "total_interest":null,"cards":[],"avalanche":{"strategy":"avalanche","order":[],"months_to_debt_free":null,\
        "debt_free_month":null,"total_interest":null,"interest_saved":null},"snowball":{"strategy":"later",\
        "order":[],"months_to_debt_free":null,"debt_free_month":null,"total_interest":null,"interest_saved":null}}
        """
        let plan = try JSONDecoder.api.decode(PayoffPlan.self, from: Data(json.utf8))
        #expect(plan.debtFreeMonth == nil)
        #expect(plan.debtFreeMonth.debtFreeText == "Never at these payments")
        #expect(plan.avalanche.savedText == nil)
        #expect(plan.snowball.strategy == .other("later"))
        #expect(plan.requestedBudget == money("40.00"))
        #expect(plan.clampedText == "Raised to cover minimums")
    }

    @Test func payoffTexts() throws {
        let plan = try payoffPlan()
        #expect(plan.debtFreeMonth.debtFreeText == "Debt-free by \(MonthRef(year: 2030, month: 10).title())")
        #expect(plan.cards[2].payoffText == "Paid off by \(MonthRef(year: 2027, month: 11).title())")
        #expect(plan.avalanche.title == "Avalanche")
        #expect(plan.snowball.subtitle == "Smallest balance first")
        #expect(plan.avalanche.savedText == "Saves \(money("1303.02").formatted())")
        let ends = CalendarDate(year: 2027, month: 4, day: 1).formatted(.dateTime.month(.abbreviated).day().year())
        #expect(plan.cards[2].promo?.payText == "Pay \(money("240.00").formatted())/mo to clear by \(ends)")
    }

    @Test func budgetSliderRunsFromThePlannedPaymentsToThreeTimesThem() throws {
        let plan = try payoffPlan()
        #expect(plan.maxBudget == money("750.00"))
        #expect(plan.budgetSteps == 50)
        #expect(plan.budget(atStep: 0) == money("250.00"))
        #expect(plan.budget(atStep: 15) == money("400.00"))
        #expect(plan.budget(atStep: 50) == money("750.00"))
        #expect(plan.budget(atStep: 99) == money("750.00"))
        #expect(plan.step(for: money("400.00")) == 15)
        #expect(plan.step(for: money("10.00")) == 0)
        #expect(plan.step(for: money("9999.00")) == 50)
    }

    @Test func promoPaceInsightKindDecodes() throws {
        let data = Data(#"["promo_pace"]"#.utf8)
        let kinds = try JSONDecoder.api.decode([InsightKind].self, from: data)
        #expect(kinds == [.promoPace])
        #expect(kinds[0].systemImage == "percent")
    }

    // MARK: Loading

    @Test func payoffEndpointSendsTheBudgetOnlyWhenSet() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        var requests = reply(200, body: try jsonFixture("payoff")).makeAsyncIterator()

        _ = try await api.payoffPlan()
        let planned = try #require(await requests.next())
        let plan = try await api.payoffPlan(budget: money("400"))
        let budgeted = try #require(await requests.next())

        #expect(planned.url?.path() == "/api/accounts/payoff")
        #expect(queryItems(planned).isEmpty)
        #expect(queryItems(budgeted) == [URLQueryItem(name: "budget", value: "400.00")])
        #expect(plan.budget == money("400.00"))
    }
}
