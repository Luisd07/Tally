import Foundation
import Testing
@testable import Tally

extension APIClientTests {
    private func loadedSummaryModel(_ api: APIClient) async throws -> KeyedValue<Int, TaxSummary> {
        _ = reply(200, body: try jsonFixture("tax-summary-2026"))
        let model = KeyedValue<Int, TaxSummary>()
        await model.load(key: 2026) { try await api.taxSummary(year: 2026) }
        return model
    }

    @Test func taxSummaryFixtureDecodes() throws {
        let summary = try JSONDecoder.api.decode(TaxSummary.self, from: jsonFixture("tax-summary-2026"))
        #expect(summary.year == 2026)
        #expect(summary.grossReceipts == Money(1500))
        #expect(summary.expensesByLine.count == 3)
        #expect(summary.expensesByLine.map(\.deductiblePct) == [100, 50, nil])
        #expect(summary.expensesByLine.map(\.displayName) == ["Travel", "Meals", "Office expense"])
        #expect(summary.uncategorizedBusiness.spent == Money(Decimal(string: "35.10")!))
        #expect(summary.uncategorizedBusiness.count == 2)
        #expect(summary.totalDeductible == Money(Decimal(string: "446.25")!))
        #expect(!summary.reimbursementsIncluded)
        #expect(summary.notes.count == 1)
    }

    @Test func liveTaxSummaryDecodes() throws {
        let summary = try JSONDecoder.api.decode(TaxSummary.self, from: jsonFixture("tax-summary-2026-live"))
        #expect(summary.expensesByLine.count == 1)
        #expect(summary.notes.count == 3)
        #expect(summary.reimbursementsIncluded)
        #expect(!summary.isEmpty)
    }

    @Test func mileageFixtureDecodesLineNineFromTheLog() throws {
        let summary = try JSONDecoder.api.decode(TaxSummary.self, from: jsonFixture("tax-summary-2026-mileage"))
        let mileage = summary.mileage
        let carTruck = try #require(summary.expensesByLine.first { $0.taxLine == TaxLineSpend.carTruck })
        #expect(carTruck.displayName == "Car and truck (standard mileage)")
        #expect(carTruck.spent == mileage.mileageAmount + mileage.parkingTolls)
        #expect(mileage.businessMiles == Miles(Decimal(string: "62.2")!))
        #expect(mileage.mileageAmount == money("46.83"))
        #expect(mileage.rates.map(\.rate.perMileText) == ["$0.725/mi", "$0.76/mi"])
        #expect(mileage.rates.map(\.miles) == [Miles(Decimal(string: "12.5")!), Miles(Decimal(string: "49.7")!)])
        #expect(mileage.rates.first?.effectiveFrom == CalendarDate(year: 2026, month: 1, day: 1))
        #expect(mileage.unpricedMiles.value == 0)
        #expect(mileage.excludedActualCarCosts.amount == 0)
        #expect(mileage.odometerStart == Miles(41250))
        #expect(mileage.totalMiles == Miles(Decimal(string: "12730.4")!))
    }

    @Test func onlyUnpricedMilesIsNotEmpty() throws {
        let summary = try JSONDecoder.api.decode(TaxSummary.self, from: jsonFixture("tax-summary-2024-unpriced"))
        #expect(summary.expensesByLine.isEmpty)
        #expect(summary.mileage.unpricedMiles == Miles(Decimal(string: "20.3")!))
        #expect(summary.mileage.rates.isEmpty)
        #expect(summary.mileage.totalMiles == nil)
        #expect(summary.mileage.hasActivity)
        #expect(!summary.isEmpty)
    }

    private func noLinesSummary(uncategorizedCount: Int) throws -> TaxSummary {
        let spent = uncategorizedCount == 0 ? "0.00" : "35.10"
        let json = """
            {"year":2026,"gross_receipts":"0.00","expenses_by_line":[],
            "uncategorized_business":{"spent":"\(spent)","count":\(uncategorizedCount)},
            "total_deductible":"0.00","reimbursements_included":false,
            "mileage":{"business_miles":"0.0","mileage_amount":"0.00","parking_tolls":"0.00","unpriced_miles":"0.0",
            "excluded_actual_car_costs":"0.00","rates":[],"odometer_start":null,"odometer_end":null,"total_miles":null},
            "notes":[]}
            """
        return try JSONDecoder.api.decode(TaxSummary.self, from: Data(json.utf8))
    }

    @Test func onlyUncategorizedSpendIsNotEmpty() throws {
        #expect(try !noLinesSummary(uncategorizedCount: 2).isEmpty)
    }

    @Test func allZeroSummaryIsEmpty() throws {
        #expect(try noLinesSummary(uncategorizedCount: 0).isEmpty)
    }

    @Test func taxSummarySendsTheYear() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let requests = reply(200, body: try jsonFixture("tax-summary-2026"))
        var iterator = requests.makeAsyncIterator()

        _ = try await api.taxSummary(year: 2026)
        let request = try #require(await iterator.next())

        #expect(request.url?.path() == "/api/tax/summary")
        #expect(queryItems(request) == [URLQueryItem(name: "year", value: "2026")])
    }

    @Test func taxSummaryModelIgnoresAStaleYearResponse() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let (arrived, arrivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let body = try jsonFixture("tax-summary-2026")
        let other = Data(String(decoding: body, as: UTF8.self).replacing("\"year\":2026", with: "\"year\":2025").utf8)
        StubURLProtocol.respond { request in
            if request.url?.query()?.contains("year=2026") == true {
                arrivedSignal.yield()
                for await _ in release { break }
                return (StubURLProtocol.response(to: request, status: 200), body)
            }
            return (StubURLProtocol.response(to: request, status: 200), other)
        }
        let model = KeyedValue<Int, TaxSummary>()

        let slow = Task { await model.load(key: 2026) { try await api.taxSummary(year: 2026) } }
        for await _ in arrived { break }
        await model.load(key: 2025) { try await api.taxSummary(year: 2025) }
        releaseSignal.yield()
        await slow.value

        #expect(model.value?.year == 2025)
    }

    @Test func failedTaxSummaryRefreshKeepsTheSummaryAndSetsError() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = try await loadedSummaryModel(api)
        _ = reply(500, body: Data(#"{"error":{"code":"internal","message":"Boom.","request_id":"r3"}}"#.utf8))

        await model.load(key: 2026) { try await api.taxSummary(year: 2026) }

        #expect(model.value?.expensesByLine.count == 3)
        #expect(model.hasData)
        #expect(model.error?.errorDescription == "Boom.")
    }

    @Test func switchingYearsDropsTheOldSummary() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = try await loadedSummaryModel(api)
        _ = reply(500, body: Data(#"{"error":{"code":"internal","message":"Boom.","request_id":"r3"}}"#.utf8))

        await model.load(key: 2025) { try await api.taxSummary(year: 2025) }

        #expect(model.value == nil)
        #expect(!model.hasData)
    }
}
