import Foundation
import Testing
@testable import Tally

/// Lives in `APIClientTests`' serialized suite because the network tests share `StubURLProtocol`'s one handler.
extension APIClientTests {
    private func cardLogJSON(
        month: String = "2026-10",
        ending: String = "-1180.25",
        endingDate: String = "2026-10-08",
        difference: String? = nil,
        hasStart: Bool = true
    ) -> Data {
        let quote: (String?) -> String = { $0.map { "\"\($0)\"" } ?? "null" }
        return Data("""
        {"month":"\(month)","starting_balance":\(hasStart ? "\"-1000.00\"" : "null"),\
        "starting_balance_date":\(hasStart ? "\"2026-09-30\"" : "null"),\
        "new_charges":"-440.00","payments":"300.00","interest_and_fees":"-40.25","other":"0.00",\
        "ending_balance":"\(ending)","ending_balance_date":"\(endingDate)","computed_ending":"-1180.25",\
        "difference":\(quote(difference))}
        """.utf8)
    }

    // MARK: Models

    @Test func cardLogFixtureDecodesToTheListedValues() throws {
        let log = try JSONDecoder.api.decode(CardLog.self, from: jsonFixture("card-log-2026-10"))
        #expect(log.month == "2026-10")
        #expect(log.newCharges == money("-440.00"))
        #expect(log.charged == money("440.00"))
        #expect(log.payments == money("300.00"))
        #expect(log.interestAndFees == money("0.00"))
        #expect(log.other == money("0.00"))
        #expect(log.endingBalance == money("-1180.25"))
        #expect(log.endingOwed == money("1180.25"))
        #expect(log.endingBalanceDate == CalendarDate(year: 2026, month: 10, day: 8))
        #expect(log.startingBalance == nil)
        #expect(log.startingOwed == nil)
        #expect(log.startingBalanceDate == nil)
        #expect(log.computedEnding == nil)
        #expect(log.difference == nil)
        #expect(!log.isReconciled)
    }

    @Test func cardLogIsReconciledOnlyWhenTheDifferenceIsZero() throws {
        let reconciled = try JSONDecoder.api.decode(CardLog.self, from: cardLogJSON(difference: "0.00"))
        #expect(reconciled.isReconciled)
        let off = try JSONDecoder.api.decode(CardLog.self, from: cardLogJSON(difference: "-12.50"))
        #expect(!off.isReconciled)
    }

    @Test func cardLogOwedHelpersNegateRawAmounts() throws {
        let log = try JSONDecoder.api.decode(CardLog.self, from: cardLogJSON(difference: "0.00"))
        #expect(log.startingOwed == money("1000.00"))
        #expect(log.feesCharged == money("40.25"))
        #expect(log.computedEndingOwed == money("1180.25"))
    }

    @Test func cardOwingMoreThanComputedHasAPositiveDifferenceOwed() throws {
        let log = try JSONDecoder.api.decode(CardLog.self, from: cardLogJSON(difference: "-12.50"))
        #expect(log.differenceOwed == money("12.50"))
        let less = try JSONDecoder.api.decode(CardLog.self, from: cardLogJSON(difference: "3.00"))
        #expect(less.differenceOwed == money("-3.00"))
    }

    @Test func cardLogFlagsAnEndingBalanceOlderThanTheMonth() throws {
        let log = try JSONDecoder.api.decode(CardLog.self, from: cardLogJSON(endingDate: "2026-09-28"))
        #expect(log.staleEndingDate(in: MonthRef(year: 2026, month: 10)) == CalendarDate(year: 2026, month: 9, day: 28))
        #expect(log.staleEndingDate(in: MonthRef(year: 2026, month: 9)) == nil)
    }

    @Test func reconciliationNamesEachState() throws {
        let october = MonthRef(year: 2026, month: 10)
        let decode = { (data: Data) in try JSONDecoder.api.decode(CardLog.self, from: data) }
        #expect(try decode(cardLogJSON(difference: "0.00")).reconciliation(in: october) == .reconciled)
        #expect(try decode(cardLogJSON(difference: "-12.50")).reconciliation(in: october) == .off(by: money("12.50")))
        #expect(
            try decode(cardLogJSON(endingDate: "2026-09-28")).reconciliation(in: october)
                == .staleEnding(CalendarDate(year: 2026, month: 9, day: 28))
        )
        #expect(
            try decode(cardLogJSON(hasStart: false)).reconciliation(in: october)
                == .missingBalances(missingStart: true, missingEnd: false)
        )
    }

    @Test func reconciliationReportsAMissingStartBeforeAStaleEnd() throws {
        let log = try JSONDecoder.api.decode(CardLog.self, from: cardLogJSON(endingDate: "2026-09-28", hasStart: false))
        #expect(log.reconciliation(in: MonthRef(year: 2026, month: 10)) == .missingBalances(missingStart: true, missingEnd: true))
    }

    @Test func reconciliationReportsAMissingEnd() throws {
        let log = try JSONDecoder.api.decode(CardLog.self, from: Data("""
        {"month":"2026-10","starting_balance":"-1000.00","starting_balance_date":"2026-09-30",\
        "new_charges":"0.00","payments":"0.00","interest_and_fees":"0.00","other":"0.00",\
        "ending_balance":null,"ending_balance_date":null,"computed_ending":null,"difference":null}
        """.utf8))
        #expect(log.reconciliation(in: MonthRef(year: 2026, month: 10)) == .missingBalances(missingStart: false, missingEnd: true))
    }

    // MARK: Loading

    @Test func cardLogEndpointSendsTheMonthToTheCardsPath() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let id = UUID()
        var requests = reply(200, body: cardLogJSON()).makeAsyncIterator()

        let log = try await api.cardLog(accountId: id, month: MonthRef(year: 2026, month: 10))
        let request = try #require(await requests.next())
        #expect(request.url?.path() == "/api/accounts/\(uuidString(id))/card-log")
        #expect(queryItems(request) == [URLQueryItem(name: "month", value: "2026-10")])
        #expect(log.month == "2026-10")
    }

    @Test func cardLogModelIgnoresAStaleMonthResponse() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = MonthValue<CardLog>()
        let (arrived, arrivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let october = cardLogJSON()
        let november = cardLogJSON(month: "2026-11")
        StubURLProtocol.respond { request in
            if request.url?.query()?.contains("2026-10") == true {
                arrivedSignal.yield()
                for await _ in release { break }
                return (StubURLProtocol.response(to: request, status: 200), october)
            }
            return (StubURLProtocol.response(to: request, status: 200), november)
        }

        let id = UUID()
        let slow = Task { await model.load(month: MonthRef(year: 2026, month: 10)) { try await api.cardLog(accountId: id, month: MonthRef(year: 2026, month: 10)) } }
        for await _ in arrived { break }
        await model.load(month: MonthRef(year: 2026, month: 11)) { try await api.cardLog(accountId: id, month: MonthRef(year: 2026, month: 11)) }
        releaseSignal.yield()
        await slow.value

        #expect(model.value?.month == "2026-11")
    }

    @Test func failedCardLogRefreshKeepsTheLogAndSetsError() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = MonthValue<CardLog>()
        let id = UUID()
        _ = reply(200, body: cardLogJSON())
        await model.load(month: MonthRef(year: 2026, month: 10)) { try await api.cardLog(accountId: id, month: MonthRef(year: 2026, month: 10)) }
        _ = reply(500, body: Data(#"{"error":{"code":"internal","message":"Boom.","request_id":"r2"}}"#.utf8))
        await model.load(month: MonthRef(year: 2026, month: 10)) { try await api.cardLog(accountId: id, month: MonthRef(year: 2026, month: 10)) }

        #expect(model.value?.newCharges == money("-440.00"))
        #expect(model.error?.errorDescription == "Boom.")
        #expect(model.hasData)
    }

    @Test func failedFirstCardLogLoadHasLoadedButNoData() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = MonthValue<CardLog>()
        _ = reply(503)
        await model.load(month: MonthRef(year: 2026, month: 10)) { try await api.cardLog(accountId: UUID(), month: MonthRef(year: 2026, month: 10)) }

        #expect(model.hasLoaded)
        #expect(!model.hasData)
        #expect(model.value == nil)
    }
}
