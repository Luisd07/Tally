import Foundation
import os
import Testing
@testable import Tally

/// Lives in `APIClientTests`' serialized suite because the network tests share `StubURLProtocol`'s one handler.
extension APIClientTests {
    private func cardJSON(
        id: UUID = UUID(),
        name: String = "Everyday Credit Card",
        balance: String? = "-1180.25",
        balanceDate: String? = "2026-10-08",
        source: String = "synced",
        dueDay: Int? = 15,
        apr: String? = "24.99",
        active: Bool = true
    ) -> String {
        let quote: (String?) -> String = { $0.map { "\"\($0)\"" } ?? "null" }
        return """
        {"account":{"id":"\(uuidString(id))","name":"\(name)","kind":"credit_card","institution":"Chase",\
        "simplefin_account_id":null,"last4":"5678","due_day":\(dueDay.map(String.init) ?? "null"),\
        "planned_payment":"100.00","apr":\(quote(apr)),"promo_apr_end":null,"active":\(active),\
        "created_at":"2026-10-01T00:00:00Z","updated_at":"2026-10-01T00:00:00Z"},\
        "balance":\(quote(balance)),"balance_date":\(quote(balanceDate)),"source_hint":"\(source)"}
        """
    }

    private func accountJSON(_ card: String) -> Data {
        let object = try! JSONSerialization.jsonObject(with: Data(card.utf8)) as! [String: Any]
        return try! JSONSerialization.data(withJSONObject: object["account"]!)
    }

    private func loadedCardsModel(_ rows: [String], includeInactive: Bool = false) async -> (CardsModel, APIClient) {
        let api = makeClient(fixture.make())
        let model = CardsModel()
        _ = reply(200, body: Data("[\(rows.joined(separator: ","))]".utf8))
        if includeInactive {
            await model.setIncludeInactive(true, api: api)
        } else {
            await model.load(api: api)
        }
        return (model, api)
    }

    private func cardFields(due: String = "", payment: String = "", apr: String = "") -> CardFields {
        CardFields(dueDayText: due, plannedPaymentText: payment, aprText: apr)
    }

    private var oct31: CalendarDate { CalendarDate(year: 2026, month: 10, day: 31) }
    private var today: CalendarDate { CalendarDate(year: 2026, month: 10, day: 19) }

    // MARK: Models

    @Test func cardsFixtureDecodesToTheListedValues() throws {
        let cards = try JSONDecoder.api.decode([CardSummary].self, from: jsonFixture("cards"))
        #expect(cards.count == 1)
        let card = cards[0]
        #expect(card.account.name == "Everyday Credit Card")
        #expect(card.account.last4 == "5678")
        #expect(card.account.dueDay == 15)
        #expect(card.account.plannedPayment == money("100.00"))
        #expect(card.account.apr == "24.99")
        #expect(card.balance == money("-1180.25"))
        #expect(card.owed == money("1180.25"))
        #expect(card.balanceDate == CalendarDate(year: 2026, month: 10, day: 8))
        #expect(card.sourceHint == .synced)
        #expect(card.id == card.account.id)
    }

    @Test func cardWithoutABalanceDecodesAndUnknownSourcesFallBack() throws {
        let data = Data("[\(cardJSON(balance: nil, balanceDate: nil, source: "plaid"))]".utf8)
        let card = try #require(JSONDecoder.api.decode([CardSummary].self, from: data).first)
        #expect(card.balance == nil)
        #expect(card.balanceDate == nil)
        #expect(card.sourceHint == .other("plaid"))
    }

    private func interestCardJSON(ytd: String, estimate: String?, mismatch: Bool) -> String {
        let base = cardJSON()
        let months = (1...12).map { "{\"month\":\"2026-\(String(format: "%02d", $0))\",\"amount\":\"\($0).50\"}" }
        let estimateJSON = estimate.map { "\"\($0)\"" } ?? "null"
        let interest = ",\"interest\":{\"months\":[\(months.joined(separator: ","))],\"year_to_date\":\"\(ytd)\","
            + "\"estimate\":\(estimateJSON),\"mismatch\":\(mismatch)}}"
        return String(base.dropLast()) + interest
    }

    @Test func cardsWithoutInterestFieldsStillDecode() throws {
        let card = try #require(JSONDecoder.api.decode([CardSummary].self, from: jsonFixture("cards")).first)
        #expect(card.interest == nil)
        #expect(card.estimateText == nil)
        #expect(!card.hasAprMismatch)
    }

    @Test func cardInterestDecodesMonthsYearToDateEstimateAndMismatch() throws {
        let data = Data("[\(interestCardJSON(ytd: "42.25", estimate: "24.58", mismatch: true))]".utf8)
        let card = try #require(JSONDecoder.api.decode([CardSummary].self, from: data).first)
        let interest = try #require(card.interest)
        #expect(interest.months.count == 12)
        #expect(interest.months[0].month == MonthRef(year: 2026, month: 1))
        #expect(interest.months[11].amount == money("12.50"))
        #expect(interest.yearToDate == money("42.25"))
        #expect(interest.estimate == money("24.58"))
        #expect(card.hasAprMismatch)
        #expect(card.estimateText == "~\(money("24.58").formatted()) interest this month")
    }

    @Test func cardInterestWithoutAnEstimateShowsNoEstimateText() throws {
        let data = Data("[\(interestCardJSON(ytd: "0.00", estimate: nil, mismatch: false))]".utf8)
        let card = try #require(JSONDecoder.api.decode([CardSummary].self, from: data).first)
        #expect(card.interest?.estimate == nil)
        #expect(card.estimateText == nil)
        #expect(!card.hasAprMismatch)
    }

    @Test func interestSummaryDecodesAndHidesAtZero() throws {
        let paid = try JSONDecoder.api.decode(
            InterestSummary.self, from: Data(#"{"year_to_date":"15.25","cards":{}}"#.utf8))
        #expect(paid.yearToDate == money("15.25"))
        #expect(paid.text == "You've paid \(money("15.25").formatted()) in interest and fees this year")
        let none = try JSONDecoder.api.decode(
            InterestSummary.self, from: Data(#"{"year_to_date":"0.00","cards":{}}"#.utf8))
        #expect(none.text == nil)
    }

    @Test func interestSummaryEndpointFeedsTheCardsModel() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t", serverURL: server)
        let api = makeClient(session)
        let paths = OSAllocatedUnfairLock(initialState: [String]())
        StubURLProtocol.respond { request in
            let path = request.url!.path()
            paths.withLock { $0.append(path) }
            let body = path.hasSuffix("/interest") ? #"{"year_to_date":"42.25","cards":{}}"# : "[]"
            return (StubURLProtocol.response(to: request, status: 200), Data(body.utf8))
        }
        let model = CardsModel()

        await model.load(api: api)

        #expect(paths.withLock { $0 }.contains("/api/accounts/interest"))
        #expect(model.interestSummary?.yearToDate == money("42.25"))
    }

    private func loanJSON(kind: String = "loan", owed: String? = "18000.00", payoffMonth: String? = "2029-03") -> String {
        let quote: (String?) -> String = { $0.map { "\"\($0)\"" } ?? "null" }
        return """
        {"account":{"id":"\(uuidString(UUID()))","name":"ACME AUTO FINANCE","kind":"\(kind)","institution":null,\
        "simplefin_account_id":"sf-1","last4":null,"due_day":10,"planned_payment":"500.00","apr":"4.90",\
        "promo_apr_end":null,"active":true,"created_at":"2026-10-01T00:00:00Z","updated_at":"2026-10-01T00:00:00Z"},\
        "owed":\(quote(owed)),"balance_date":"2026-10-08","months_to_payoff":41,"payoff_month":\(quote(payoffMonth)),\
        "total_interest":"1900.00"}
        """
    }

    @Test func loanKindDecodesAndUnknownKindsStillDecode() throws {
        let loan = try JSONDecoder.api.decode(LoanSummary.self, from: Data(loanJSON().utf8))
        #expect(loan.account.kind == .loan)
        #expect(loan.owed == money("18000.00"))
        #expect(loan.payoffText?.hasPrefix("Paid off by") == true)
        let unknown = try JSONDecoder.api.decode(LoanSummary.self, from: Data(loanJSON(kind: "heloc").utf8))
        #expect(unknown.account.kind == .other("heloc"))
        let never = try JSONDecoder.api.decode(LoanSummary.self, from: Data(loanJSON(payoffMonth: nil).utf8))
        #expect(never.payoffText == "Never at its payment")
    }

    @Test func loansLoadSeparatelyFromCards() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t", serverURL: server)
        let api = makeClient(session)
        let card = cardJSON()
        let loan = loanJSON()
        StubURLProtocol.respond { request in
            let path = request.url!.path()
            let body = path.hasSuffix("/loans") ? "[\(loan)]" : path.hasSuffix("/cards") ? "[\(card)]" : #"{"year_to_date":"0.00","cards":{}}"#
            return (StubURLProtocol.response(to: request, status: 200), Data(body.utf8))
        }
        let model = CardsModel()

        await model.load(api: api)

        #expect(model.cards.count == 1)
        #expect(model.cards.first?.account.kind == .creditCard)
        #expect(model.loans.count == 1)
        #expect(model.loans.first?.account.name == "ACME AUTO FINANCE")
    }

    @Test func cardPatchedToALoanLandsInLoansOnceUpdateReturns() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t", serverURL: server)
        let api = makeClient(session)
        let id = UUID()
        let card = cardJSON(id: id)
        let loan = loanJSON().replacingOccurrences(of: "ACME AUTO FINANCE", with: "Everyday Credit Card")
        let patched = String(data: accountJSON(loan), encoding: .utf8)!
        let converted = OSAllocatedUnfairLock(initialState: false)
        StubURLProtocol.respond { request in
            let path = request.url!.path()
            let done = converted.withLock { $0 }
            let body: String
            if request.httpMethod == "PATCH" {
                converted.withLock { $0 = true }
                body = patched
            } else if path.hasSuffix("/loans") {
                body = done ? "[\(loan)]" : "[]"
            } else if path.hasSuffix("/cards") {
                body = done ? "[]" : "[\(card)]"
            } else {
                body = #"{"year_to_date":"0.00","cards":{}}"#
            }
            return (StubURLProtocol.response(to: request, status: 200), Data(body.utf8))
        }
        let model = CardsModel()
        await model.load(api: api)
        let before = try #require(model.cards.first)
        #expect(model.loans.isEmpty)

        let saved = await model.update(before, patch: AccountPatch(kind: .loan), api: api)

        #expect(saved)
        #expect(model.cards.isEmpty)
        #expect(model.loans.count == 1)
    }

    @Test func loanToggleSendsTheKindAndSwitchingBackSendsCreditCard() throws {
        let card = try #require(JSONDecoder.api.decode([CardSummary].self, from: Data("[\(cardJSON())]".utf8)).first)
        var fields = CardFields(editing: card)
        #expect(!fields.isLoan)
        fields.isLoan = true
        let toLoanForm = try fields.validated.get()
        let toLoan = try #require(AccountPatch(from: card, to: toLoanForm))
        #expect(try snakeCaseKeys(toLoan)["kind"] as? String == "loan")

        let loan = try JSONDecoder.api.decode(LoanSummary.self, from: Data(loanJSON().utf8)).asCard
        var back = CardFields(editing: loan)
        #expect(back.isLoan)
        let unchanged = try back.validated.get()
        #expect(AccountPatch(from: loan, to: unchanged) == nil)
        back.isLoan = false
        let toCardForm = try back.validated.get()
        let toCard = try #require(AccountPatch(from: loan, to: toCardForm))
        #expect(try snakeCaseKeys(toCard)["kind"] as? String == "credit_card")
    }

    @Test func accountPatchSendsOnlySetKeysAndNullsClears() throws {
        #expect(try snakeCaseKeys(AccountPatch()).isEmpty)
        let cleared = try snakeCaseKeys(AccountPatch(dueDay: .some(nil), apr: .some(nil)))
        #expect(Set(cleared.keys) == ["apr", "due_day"])
        #expect(cleared["apr"] is NSNull)
        #expect(cleared["due_day"] is NSNull)
        let set = try snakeCaseKeys(AccountPatch(
            dueDay: .some(20), plannedPayment: .some(money("75")), apr: .some("19.90"),
            promoAprEnd: .some(oct31), active: false
        ))
        #expect(Set(set.keys) == ["due_day", "planned_payment", "apr", "promo_apr_end", "active"])
        #expect(set["due_day"] as? Int == 20)
        #expect(set["planned_payment"] as? String == "75.00")
        #expect(set["apr"] as? String == "19.90")
        #expect(set["promo_apr_end"] as? String == "2026-10-31")
        #expect(set["active"] as? Bool == false)
    }

    @Test func cardFieldsValidateEachRule() throws {
        let cases: [(CardFields, CardForm.Invalid)] = [
            (cardFields(due: "0"), .dueDay),
            (cardFields(due: "32"), .dueDay),
            (cardFields(due: "1.5"), .dueDay),
            (cardFields(due: "x"), .dueDay),
            (cardFields(payment: "-5"), .plannedPayment),
            (cardFields(payment: "12abc"), .plannedPayment),
            (cardFields(apr: "100.01"), .apr),
            (cardFields(apr: "24.999"), .apr),
            (cardFields(apr: "-1"), .apr),
        ]
        for (fields, rule) in cases {
            #expect(fields.validated == .failure(rule))
        }
        #expect(Set([CardForm.Invalid.dueDay, .plannedPayment, .apr].map(\.message)).count == 3)

        let blank = try cardFields().validated.get()
        #expect(blank == CardForm(dueDay: nil, plannedPayment: nil, apr: nil, promoAprEnd: nil))
        let ok = try cardFields(due: " 31 ", payment: "1,000", apr: "100").validated.get()
        #expect(ok.dueDay == 31)
        #expect(ok.plannedPayment == money("1000"))
        #expect(ok.apr?.inputText == "100.00")
        #expect(try cardFields(payment: "0", apr: "0.5").validated.get().apr?.inputText == "0.50")
    }

    @Test func cardFieldsOfACardReadBackAsNoPatch() throws {
        let card = try #require(JSONDecoder.api.decode([CardSummary].self, from: jsonFixture("cards")).first)
        let fields = CardFields(editing: card)
        #expect(fields.aprText == "24.99")
        #expect(fields.plannedPaymentText == "100.00")
        #expect(AccountPatch(from: card, to: try fields.validated.get()) == nil)
    }

    @Test func accountPatchFromFormSendsChangedFieldsAndNullsCleared() throws {
        let card = try #require(JSONDecoder.api.decode([CardSummary].self, from: jsonFixture("cards")).first)
        var fields = CardFields(editing: card)
        fields.dueDayText = ""
        fields.aprText = "19.9"
        let patch = try #require(AccountPatch(from: card, to: try fields.validated.get()))
        let json = try snakeCaseKeys(patch)
        #expect(Set(json.keys) == ["due_day", "apr"])
        #expect(json["due_day"] is NSNull)
        #expect(json["apr"] as? String == "19.90")
    }

    @Test func balanceFieldsValidateAmountAndDate() throws {
        func fields(_ amount: String, credit: Bool = false, date: CalendarDate? = nil) -> BalanceFields {
            BalanceFields(amountText: amount, isCredit: credit, date: date ?? oct31)
        }
        let late = CalendarDate(year: 2026, month: 10, day: 31)
        #expect(fields("", date: late).validated(today: late) == .failure(.amount))
        #expect(fields("-5", date: late).validated(today: late) == .failure(.amount))
        #expect(fields("5", date: late).validated(today: today) == .failure(.date))
        #expect(fields("5", date: CalendarDate(year: 1999, month: 12, day: 31)).validated(today: today) == .failure(.date))

        let debt = try fields("250", date: today).validated(today: today).get()
        #expect(debt.balance == money("-250"))
        let credit = try fields("1,250.5", credit: true, date: CalendarDate(year: 2000, month: 1, day: 1)).validated(today: today).get()
        #expect(credit.balance == money("1250.50"))
    }

    // MARK: Endpoints

    @Test func cardsEndpointSendsIncludeInactiveOnlyWhenAsked() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        var requests = reply(200, body: Data("[]".utf8)).makeAsyncIterator()

        _ = try await api.cards()
        _ = try await api.cards(includeInactive: true)
        let plain = try #require(await requests.next())
        let all = try #require(await requests.next())
        #expect(plain.url?.path() == "/api/accounts/cards")
        #expect(queryItems(plain).isEmpty)
        #expect(queryItems(all) == [URLQueryItem(name: "include_inactive", value: "true")])
    }

    @Test func updateAccountPatchesTheIdWithAnAprNull() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let id = UUID()
        var requests = reply(200, body: accountJSON(cardJSON(id: id, apr: nil))).makeAsyncIterator()

        let saved = try await api.updateAccount(id: id, patch: AccountPatch(apr: .some(nil)))
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "PATCH")
        #expect(request.url?.path() == "/api/accounts/\(uuidString(id))")
        let body = try #require(bodyData(request))
        #expect(try JSONSerialization.jsonObject(with: body) as? NSDictionary == ["apr": NSNull()] as NSDictionary)
        #expect(saved.apr == nil)
    }

    @Test func upsertBalancePutsTheDayInThePathAndOnlyTheBalance() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let id = UUID()
        let snapshot = """
        {"id":"\(uuidString(UUID()))","account_id":"\(uuidString(id))","date":"2026-10-31","balance":"-250.00",\
        "created_at":"2026-10-19T00:00:00Z","updated_at":"2026-10-19T00:00:00Z"}
        """
        var requests = reply(200, body: Data(snapshot.utf8)).makeAsyncIterator()

        let saved = try await api.upsertBalance(accountId: id, date: oct31, balance: money("-250"))
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "PUT")
        #expect(request.url?.path() == "/api/accounts/\(uuidString(id))/balances/2026-10-31")
        #expect(try String(decoding: #require(bodyData(request)), as: UTF8.self) == #"{"balance":"-250.00"}"#)
        #expect(saved.date == oct31)
        #expect(saved.balance == money("-250.00"))
    }

    // MARK: Loading and edits

    @Test func cardsIncludeInactiveReloadsWithTheQuery() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = CardsModel()
        var requests = reply(200, body: Data("[\(cardJSON(active: false))]".utf8)).makeAsyncIterator()

        await model.setIncludeInactive(true, api: api)
        let request = try #require(await requests.next())
        #expect(queryItems(request) == [URLQueryItem(name: "include_inactive", value: "true")])
        #expect(model.cards.count == 1)

        _ = reply(200, body: Data("[]".utf8))
        await model.setIncludeInactive(false, api: api)
        #expect(model.cards.isEmpty)
        #expect(!model.includeInactive)
    }

    @Test func staleCardsLoadIsIgnored() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = CardsModel()
        let (arrived, arrivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let old = Data("[\(cardJSON(name: "Old"))]".utf8)
        let new = Data("[\(cardJSON(name: "New"))]".utf8)
        StubURLProtocol.respond { request in
            if request.url?.query() == nil {
                arrivedSignal.yield()
                for await _ in release { break }
                return (StubURLProtocol.response(to: request, status: 200), old)
            }
            return (StubURLProtocol.response(to: request, status: 200), new)
        }

        let slow = Task { await model.load(api: api) }
        for await _ in arrived { break }
        await model.setIncludeInactive(true, api: api)
        releaseSignal.yield()
        await slow.value

        #expect(model.cards.map(\.account.name) == ["New"])
    }

    @Test func failedCardsRefreshKeepsRowsAndSetsError() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api) = await loadedCardsModel([cardJSON()])
        _ = reply(500, body: Data(#"{"error":{"code":"internal","message":"Boom.","request_id":"r2"}}"#.utf8))
        await model.load(api: api)

        #expect(model.cards.count == 1)
        #expect(model.error?.errorDescription == "Boom.")
        #expect(model.hasData)
    }

    @Test func dueDayEditShowsAtOnceThenRollsBackOnA422() async throws {
        defer { try? fixture.cleanUp() }
        let id = UUID()
        let (model, api) = await loadedCardsModel([cardJSON(id: id, dueDay: 15)])
        let (arrived, arrivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        StubURLProtocol.respond { request in
            arrivedSignal.yield()
            for await _ in release { break }
            return (
                StubURLProtocol.response(to: request, status: 422),
                Data(#"{"error":{"code":"validation_error","message":"Due day is invalid.","request_id":"r3"}}"#.utf8)
            )
        }

        let card = try #require(model.cards.first)
        let edit = Task { await model.update(card, patch: AccountPatch(dueDay: .some(20)), api: api) }
        for await _ in arrived { break }
        #expect(model.cards.first?.account.dueDay == 20)
        releaseSignal.yield()
        let saved = await edit.value

        #expect(!saved)
        #expect(model.cards.first?.account.dueDay == 15)
        #expect(model.editError?.error.errorDescription == "Due day is invalid.")
        #expect(model.editError?.rowName == "Everyday Credit Card")
    }

    @Test func inactivatedCardLeavesTheListUnlessInactiveAreShown() async throws {
        defer { try? fixture.cleanUp() }
        let id = UUID()
        let (model, api) = await loadedCardsModel([cardJSON(id: id)])
        _ = reply(200, body: accountJSON(cardJSON(id: id, active: false)))
        let saved = await model.update(try #require(model.cards.first), patch: AccountPatch(active: false), api: api)
        #expect(saved)
        #expect(model.cards.isEmpty)

        let (shown, shownApi) = await loadedCardsModel([cardJSON(id: id)], includeInactive: true)
        _ = reply(200, body: accountJSON(cardJSON(id: id, active: false)))
        let shownSaved = await shown.update(try #require(shown.cards.first), patch: AccountPatch(active: false), api: shownApi)
        #expect(shownSaved)
        #expect(shown.cards.map(\.id) == [id])
        #expect(shown.cards.first?.account.active == false)
    }

    @Test func savedEditKeepsTheBalance() async throws {
        defer { try? fixture.cleanUp() }
        let id = UUID()
        let (model, api) = await loadedCardsModel([cardJSON(id: id, dueDay: 15)])
        _ = reply(200, body: accountJSON(cardJSON(id: id, balance: nil, dueDay: 20)))
        await model.update(try #require(model.cards.first), patch: AccountPatch(dueDay: .some(20)), api: api)

        let card = try #require(model.cards.first)
        #expect(card.account.dueDay == 20)
        #expect(card.balance == money("-1180.25"))
    }

    private func snapshotJSON(_ account: UUID, date: String, balance: String) -> Data {
        Data("""
        {"id":"\(uuidString(UUID()))","account_id":"\(uuidString(account))","date":"\(date)","balance":"\(balance)",\
        "created_at":"2026-10-19T00:00:00Z","updated_at":"2026-10-19T00:00:00Z"}
        """.utf8)
    }

    @Test func recordedBalanceOnANewerDayUpdatesTheCard() async throws {
        defer { try? fixture.cleanUp() }
        let id = UUID()
        let (model, api) = await loadedCardsModel([cardJSON(id: id, source: "manual")])
        _ = reply(200, body: snapshotJSON(id, date: "2026-10-31", balance: "-250.00"))
        let entry = try BalanceFields(amountText: "250", date: oct31).validated(today: oct31).get()
        let recorded = await model.recordBalance(try #require(model.cards.first), entry: entry, api: api)

        #expect(recorded)
        #expect(model.cards.first?.balance == money("-250.00"))
        #expect(model.cards.first?.balanceDate == oct31)
        #expect(model.balanceError == nil)
    }

    @Test func recordedBalanceOnAnOlderDayLeavesTheCard() async throws {
        defer { try? fixture.cleanUp() }
        let id = UUID()
        let (model, api) = await loadedCardsModel([cardJSON(id: id, source: "manual")])
        _ = reply(200, body: snapshotJSON(id, date: "2026-09-30", balance: "-90.00"))
        let older = CalendarDate(year: 2026, month: 9, day: 30)
        let entry = try BalanceFields(amountText: "90", date: older).validated(today: oct31).get()
        let recorded = await model.recordBalance(try #require(model.cards.first), entry: entry, api: api)

        #expect(recorded)
        #expect(model.cards.first?.balance == money("-1180.25"))
        #expect(model.cards.first?.balanceDate == CalendarDate(year: 2026, month: 10, day: 8))
    }

    @Test func failedBalanceKeepsTheCardAndSaysWhy() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api) = await loadedCardsModel([cardJSON(source: "manual")])
        _ = reply(409, body: Data(#"{"error":{"code":"conflict","message":"This account is synced.","request_id":"r4"}}"#.utf8))
        let entry = try BalanceFields(amountText: "250", date: oct31).validated(today: oct31).get()
        let recorded = await model.recordBalance(try #require(model.cards.first), entry: entry, api: api)

        #expect(!recorded)
        #expect(model.cards.first?.balance == money("-1180.25"))
        #expect(model.balanceError?.errorDescription == "This account is synced.")
    }

    /// Records a balance while a due-day edit on the same card is held, then lets the edit succeed or fail.
    private func balanceRecordedDuringAnEdit(editSucceeds: Bool) async throws -> CardsModel {
        let id = UUID()
        let (model, api) = await loadedCardsModel([cardJSON(id: id, source: "manual", dueDay: 15)])
        let snapshot = String(decoding: snapshotJSON(id, date: "2026-10-31", balance: "-250.00"), as: UTF8.self)
        let saved = (status: 200, body: String(decoding: accountJSON(cardJSON(id: id, dueDay: 20)), as: UTF8.self))
        let (arrived, release) = holdPatches([id: editSucceeds ? saved : validationError], others: { _ in snapshot })

        let card = try #require(model.cards.first)
        let edit = Task { await model.update(card, patch: AccountPatch(dueDay: .some(20)), api: api) }
        await waitForPatches(1, in: arrived)
        let entry = try BalanceFields(amountText: "250", date: oct31).validated(today: oct31).get()
        let recorded = await model.recordBalance(card, entry: entry, api: api)
        #expect(recorded)
        #expect(model.cards.first?.balance == money("-250.00"))
        release[id]?.yield()
        #expect(await edit.value == editSucceeds)
        return model
    }

    @Test func balanceRecordedDuringAnEditStaysWhenTheEditSaves() async throws {
        defer { try? fixture.cleanUp() }
        let model = try await balanceRecordedDuringAnEdit(editSucceeds: true)
        #expect(model.cards.first?.account.dueDay == 20)
        #expect(model.cards.first?.balance == money("-250.00"))
        #expect(model.cards.first?.balanceDate == oct31)
    }

    @Test func balanceRecordedDuringAnEditStaysWhenTheEditFails() async throws {
        defer { try? fixture.cleanUp() }
        let model = try await balanceRecordedDuringAnEdit(editSucceeds: false)
        #expect(model.cards.first?.account.dueDay == 15)
        #expect(model.cards.first?.balance == money("-250.00"))
        #expect(model.cards.first?.balanceDate == oct31)
    }
}

/// The pure date and display helpers behind the Cards screen, on a fixed Gregorian calendar.
@MainActor
struct CardDisplayTests {
    private let calendar = Calendar(identifier: .gregorian)
    private let today = CalendarDate(year: 2026, month: 10, day: 19)

    private func nextDue(day: Int, from today: CalendarDate) -> CalendarDate {
        CalendarDate.nextOccurrence(ofDay: day, onOrAfter: today)
    }

    private func card(balance: String?, promoEnd: CalendarDate? = nil) -> CardSummary {
        let account = Account(
            id: UUID(), name: "Card", kind: .creditCard, institution: nil, simplefinAccountId: nil, last4: nil,
            dueDay: nil, plannedPayment: nil, apr: nil, promoAprEnd: promoEnd, active: true,
            createdAt: .now, updatedAt: .now
        )
        return CardSummary(
            account: account, balance: balance.map { Money(Decimal(string: $0)!) }, balanceDate: nil, sourceHint: .manual
        )
    }

    /// The day `offset` days after `today`.
    private func day(_ offset: Int) -> CalendarDate {
        CalendarDate(date: calendar.date(byAdding: .day, value: offset, to: today.date(in: calendar)!)!, calendar: calendar)
    }

    private func promoText(endingIn days: Int?) -> String? {
        card(balance: nil, promoEnd: days.map { day($0) }).promoText(today: today, calendar: calendar)
    }

    @Test func dueDayThirtyOneLandsOnTheLastDayOfAThirtyDayMonth() {
        #expect(nextDue(day: 31, from: CalendarDate(year: 2026, month: 11, day: 5)) == CalendarDate(year: 2026, month: 11, day: 30))
        #expect(nextDue(day: 31, from: CalendarDate(year: 2026, month: 2, day: 5)) == CalendarDate(year: 2026, month: 2, day: 28))
        #expect(nextDue(day: 30, from: CalendarDate(year: 2028, month: 2, day: 5)) == CalendarDate(year: 2028, month: 2, day: 29))
        #expect(nextDue(day: 29, from: CalendarDate(year: 2100, month: 2, day: 5)) == CalendarDate(year: 2100, month: 2, day: 28))
    }

    @Test func dueDayAlreadyPassedMovesToNextMonth() {
        #expect(nextDue(day: 15, from: CalendarDate(year: 2026, month: 10, day: 16)) == CalendarDate(year: 2026, month: 11, day: 15))
        #expect(nextDue(day: 15, from: CalendarDate(year: 2026, month: 12, day: 20)) == CalendarDate(year: 2027, month: 1, day: 15))
        #expect(nextDue(day: 31, from: CalendarDate(year: 2026, month: 11, day: 1)) == CalendarDate(year: 2026, month: 11, day: 30))
    }

    @Test func todayIsTheDueDayStaysToday() {
        #expect(nextDue(day: 15, from: CalendarDate(year: 2026, month: 10, day: 15)) == CalendarDate(year: 2026, month: 10, day: 15))
    }

    @Test func daysUntilCountsWholeDays() {
        let today = CalendarDate(year: 2026, month: 10, day: 30)
        #expect(today.days(until: CalendarDate(year: 2026, month: 11, day: 2), calendar: calendar) == 3)
        #expect(today.days(until: CalendarDate(year: 2026, month: 10, day: 28), calendar: calendar) == -2)
    }

    @Test func balanceTextShowsDebtOwedAndACreditAsCredit() {
        let debt = card(balance: "-1180.25").balanceText
        #expect(debt == Money(Decimal(string: "1180.25")!).formatted())
        #expect(debt.contains("1,180.25"))
        #expect(!debt.contains("Credit"))
        #expect(!debt.contains("-"))
        #expect(card(balance: "50").balanceText == "Credit \(Money(50).formatted())")
        #expect(card(balance: "50").balanceText.contains("$50.00"))
        #expect(card(balance: nil).balanceText == "No balance yet")
    }

    @Test func promoTextCountsTheDaysLeftUntilItEnds() {
        #expect(promoText(endingIn: nil) == nil)
        #expect(promoText(endingIn: -1) == "Promo ended")
        #expect(promoText(endingIn: 0) == "Promo ends \(day(0).formatted()) · ends today")
        #expect(promoText(endingIn: 1) == "Promo ends \(day(1).formatted()) · 1 day left")
        #expect(promoText(endingIn: 12) == "Promo ends \(day(12).formatted()) · 12 days left")
    }

    @Test func latestBalanceDateIsTheEarlierOfLocalAndUTCToday() throws {
        var tokyo = Calendar(identifier: .gregorian)
        tokyo.timeZone = try #require(TimeZone(identifier: "Asia/Tokyo"))
        var losAngeles = Calendar(identifier: .gregorian)
        losAngeles.timeZone = try #require(TimeZone(identifier: "America/Los_Angeles"))
        let utcEvening = try Date("2026-10-19T20:00:00Z", strategy: .iso8601)
        let utcEarly = try Date("2026-10-20T03:00:00Z", strategy: .iso8601)

        #expect(BalanceFields.latestDate(now: utcEvening, calendar: tokyo) == today)
        #expect(BalanceFields.latestDate(now: utcEarly, calendar: losAngeles) == today)
        #expect(BalanceFields.latestDate(now: utcEvening, calendar: losAngeles) == today)
        #expect(BalanceFields.latestDate(now: utcEarly, calendar: tokyo) == CalendarDate(year: 2026, month: 10, day: 20))
    }
}
