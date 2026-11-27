import Foundation
import os
import Testing
@testable import Tally

/// Lives in `APIClientTests`' serialized suite because the network tests share `StubURLProtocol`'s one handler.
extension APIClientTests {
    private func tripJSON(
        id: UUID,
        date: String = "2026-10-01",
        miles: String = "12.5",
        purpose: String = "Client meeting",
        start: String? = nil,
        end: String? = nil,
        createdAt: String = "2026-10-01T00:00:00Z"
    ) -> String {
        let quote: (String?) -> String = { $0.map { "\"\($0)\"" } ?? "null" }
        return """
        {"id":"\(uuidString(id))","date":"\(date)","miles":"\(miles)","purpose":"\(purpose)",\
        "start_location":\(quote(start)),"end_location":\(quote(end)),\
        "created_at":"\(createdAt)","updated_at":"\(createdAt)"}
        """
    }

    private func savedTripJSON(id: UUID, name: String = "Office run", miles: String = "8.2") -> String {
        """
        {"id":"\(uuidString(id))","name":"\(name)","miles":"\(miles)","purpose":"Drop off documents",\
        "start_location":"Home","end_location":"Office",\
        "created_at":"2026-10-01T00:00:00Z","updated_at":"2026-10-01T00:00:00Z"}
        """
    }

    private func rateJSON(id: UUID, from: String, rate: String) -> String {
        """
        {"id":"\(uuidString(id))","effective_from":"\(from)","rate":"\(rate)",\
        "created_at":"2026-10-01T00:00:00Z","updated_at":"2026-10-01T00:00:00Z"}
        """
    }

    private func jsonArray(_ rows: [String]) -> Data {
        Data("[\(rows.joined(separator: ","))]".utf8)
    }

    private func errorJSON(_ code: String, _ message: String) -> Data {
        Data(#"{"error":{"code":"\#(code)","message":"\#(message)","request_id":"r1"}}"#.utf8)
    }

    private func mileageRequestJSON(_ request: URLRequest) throws -> [String: Any] {
        let body = try #require(bodyData(request))
        return try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
    }

    private func decodedTrip(_ json: String) throws -> Trip {
        try JSONDecoder.api.decode(Trip.self, from: Data(json.utf8))
    }

    private func decodedSavedTrip(_ json: String) throws -> SavedTrip {
        try JSONDecoder.api.decode(SavedTrip.self, from: Data(json.utf8))
    }

    /// Answers requests in turn with `answers`, the last one repeating, and streams each request it saw.
    private func replyInTurn(_ answers: [(status: Int, body: Data)]) -> AsyncStream<URLRequest> {
        let (requests, seen) = AsyncStream.makeStream(of: URLRequest.self)
        let next = OSAllocatedUnfairLock(initialState: 0)
        StubURLProtocol.respond { request in
            seen.yield(request)
            let index = next.withLock { count in
                defer { count += 1 }
                return min(count, answers.count - 1)
            }
            return (StubURLProtocol.response(to: request, status: answers[index].status), answers[index].body)
        }
        return requests
    }

    private func loadedMileageModel(_ rows: [String], year: Int = 2026) async -> (MileageModel, APIClient) {
        let api = makeClient(fixture.make())
        let model = MileageModel()
        _ = reply(200, body: jsonArray(rows))
        await model.load(year: year, api: api)
        return (model, api)
    }

    // MARK: Trips

    @Test func mileageLoadsTheYearsTripsNewestFirstWithTheirMiles() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = (0..<2).map { _ in UUID() }
        let model = MileageModel()
        var requests = reply(200, body: jsonArray([
            tripJSON(id: ids[0], date: "2026-10-01", miles: "12.5"),
            tripJSON(id: ids[1], date: "2026-03-01", miles: "40"),
        ])).makeAsyncIterator()

        await model.load(year: 2026, api: api)
        let request = try #require(await requests.next())
        #expect(request.url?.path() == "/api/mileage/trips")
        #expect(queryItems(request) == [URLQueryItem(name: "year", value: "2026")])
        #expect(model.trips.map(\.id) == ids)
        #expect(model.businessMiles == Miles(Decimal(string: "52.5")!))
        #expect(model.hasData)
    }

    @Test func switchingYearsClearsTheTripsBeforeTheNewYearLands() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api) = await loadedMileageModel([tripJSON(id: UUID())])
        _ = reply(500, body: errorJSON("internal", "Down."))
        await model.load(year: 2025, api: api)
        #expect(model.trips.isEmpty)
        #expect(!model.hasData)
        #expect(model.error?.status == 500)
    }

    @Test func createdTripIsListedInDateOrderOnlyInItsYear() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<2).map { _ in UUID() }
        let (model, api) = await loadedMileageModel([
            tripJSON(id: ids[0], date: "2026-10-01"),
            tripJSON(id: ids[1], date: "2026-03-01"),
        ])
        let form = try TripFields(
            date: CalendarDate(year: 2026, month: 6, day: 1),
            details: TripDetailFields(milesText: "8.2", purpose: " Bank ", startLocation: "Home")
        ).validated.get()
        let draft = TripDraft(id: UUID(), form: form)
        var requests = reply(201, body: Data(tripJSON(id: draft.id, date: "2026-06-01", miles: "8.2").utf8))
            .makeAsyncIterator()

        #expect(await model.create(draft, api: api))
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "POST")
        #expect(request.url?.path() == "/api/mileage/trips")
        let json = try mileageRequestJSON(request)
        #expect(Set(json.keys) == ["id", "date", "miles", "purpose", "start_location"])
        #expect(json["id"] as? String == draft.id.uuidString)
        #expect(json["miles"] as? String == "8.2")
        #expect(json["purpose"] as? String == "Bank")
        #expect(model.trips.map(\.id) == [ids[0], draft.id, ids[1]])

        let lastYear = TripDraft(id: UUID(), date: CalendarDate(year: 2025, month: 6, day: 1), miles: Miles(5), purpose: "Bank")
        _ = reply(201, body: Data(tripJSON(id: lastYear.id, date: "2025-06-01").utf8))
        #expect(await model.create(lastYear, api: api))
        #expect(model.trips.count == 3)
    }

    @Test func failedTripCreateSetsCreateErrorAndListsNothing() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api) = await loadedMileageModel([])
        _ = reply(422, body: errorJSON("validation_error", "Nope."))
        let draft = TripDraft(date: CalendarDate(year: 2026, month: 6, day: 1), miles: Miles(5), purpose: "Bank")
        #expect(!(await model.create(draft, api: api)))
        #expect(model.createError?.localizedDescription == "Nope.")
        #expect(model.trips.isEmpty)
    }

    @Test func editedTripSendsOnlyTheChangedFieldsAndShowsAtOnce() async throws {
        defer { try? fixture.cleanUp() }
        let id = UUID()
        let original = tripJSON(id: id, miles: "12.5", start: "Home", end: "Office")
        let (model, api) = await loadedMileageModel([original])
        let trip = model.trips[0]
        var fields = TripFields(editing: trip)
        fields.details.milesText = "14"
        fields.details.startLocation = "  "
        let patch = try #require(TripPatch(from: trip, to: try fields.validated.get()))
        #expect(patch == TripPatch(miles: Miles(14), startLocation: .some(nil)))
        var requests = reply(200, body: Data(tripJSON(id: id, miles: "14.0", end: "Office").utf8)).makeAsyncIterator()

        await model.update(trip, patch: patch, api: api)
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "PATCH")
        #expect(request.url?.path() == "/api/mileage/trips/\(uuidString(id))")
        let json = try mileageRequestJSON(request)
        #expect(Set(json.keys) == ["miles", "start_location"])
        #expect(json["miles"] as? String == "14.0")
        #expect(json["start_location"] is NSNull)
        #expect(model.trips[0].miles == Miles(14))
        #expect(model.trips[0].startLocation == nil)
        #expect(model.editError == nil)
    }

    @Test func unchangedTripFormMakesNoPatch() throws {
        let trip = try decodedTrip(tripJSON(id: UUID(), purpose: " Client meeting ", start: "Home"))
        #expect(TripPatch(from: trip, to: try TripFields(editing: trip).validated.get()) == nil)
    }

    @Test func tripMovedToAnotherYearLeavesTheList() async throws {
        defer { try? fixture.cleanUp() }
        let id = UUID()
        let (model, api) = await loadedMileageModel([tripJSON(id: id)])
        _ = reply(200, body: Data(tripJSON(id: id, date: "2025-12-30").utf8))
        await model.update(model.trips[0], patch: TripPatch(date: CalendarDate(year: 2025, month: 12, day: 30)), api: api)
        #expect(model.trips.isEmpty)
    }

    @Test func failedTripEditRollsBackWithTheServersMessage() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api) = await loadedMileageModel([tripJSON(id: UUID(), miles: "12.5")])
        _ = reply(422, body: errorJSON("validation_error", "Nope."))
        await model.update(model.trips[0], patch: TripPatch(miles: Miles(3)), api: api)
        #expect(model.trips[0].miles == Miles(Decimal(string: "12.5")!))
        #expect(model.editError?.message == "Client meeting: Nope.")
    }

    @Test func deletedTripLeavesTheListAndAFailedDeleteComesBack() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<2).map { _ in UUID() }
        let (model, api) = await loadedMileageModel([
            tripJSON(id: ids[0], date: "2026-10-01"),
            tripJSON(id: ids[1], date: "2026-03-01"),
        ])
        var requests = reply(204).makeAsyncIterator()
        await model.delete(model.trips[0], api: api)
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "DELETE")
        #expect(request.url?.path() == "/api/mileage/trips/\(uuidString(ids[0]))")
        #expect(model.trips.map(\.id) == [ids[1]])

        _ = reply(500, body: errorJSON("internal", "Down."))
        await model.delete(model.trips[0], api: api)
        #expect(model.trips.map(\.id) == [ids[1]])
        #expect(model.editError?.message == "Client meeting: Down.")
    }

    // MARK: Validation

    @Test func tripFieldsNeedMilesAboveZeroWithOneDecimalAndAPurpose() throws {
        func rule(_ miles: String, _ purpose: String = "Bank", start: String = "", end: String = "") -> MileageFormRule? {
            let fields = TripFields(details: TripDetailFields(milesText: miles, purpose: purpose, startLocation: start, endLocation: end))
            if case .failure(let rule) = fields.validated { return rule }
            return nil
        }
        #expect(rule("") == .miles)
        #expect(rule("0") == .miles)
        #expect(rule("0.0") == .miles)
        #expect(rule("-3") == .miles)
        #expect(rule("12.55") == .miles)
        #expect(rule("12a") == .miles)
        #expect(rule("12.5", "   ") == .purpose)
        #expect(rule("12.5", String(repeating: "a", count: 201)) == .purpose)
        #expect(rule("12.5", start: String(repeating: "a", count: 201)) == .location)
        #expect(rule("12.5", end: String(repeating: "a", count: 201)) == .location)
        #expect(rule("0.1") == nil)
        #expect(rule("1,250.5") == nil)

        let form = try TripFields(details: TripDetailFields(milesText: " 12.5 ", purpose: " Bank ", startLocation: " ", endLocation: " Office "))
            .validated.get()
        #expect(form.details == TripDetails(miles: Miles(Decimal(string: "12.5")!), purpose: "Bank", startLocation: nil, endLocation: "Office"))
        #expect(form.date == CalendarDate(date: .now))
        #expect(MileageFormRule.miles.message == "Enter miles above zero with at most one decimal, like 12.5.")
    }

    @Test func savedTripFieldsAlsoNeedAName() throws {
        let details = TripDetailFields(milesText: "8.2", purpose: "Bank")
        #expect(SavedTripFields(name: "  ", details: details).validated == .failure(.name))
        #expect(SavedTripFields(name: String(repeating: "a", count: 101), details: details).validated == .failure(.name))
        #expect(SavedTripFields(name: "Run", details: TripDetailFields(milesText: "0", purpose: "Bank")).validated == .failure(.miles))
        #expect(try SavedTripFields(name: " Run ", details: details).validated.get().name == "Run")

        let saved = try decodedSavedTrip(savedTripJSON(id: UUID()))
        var fields = SavedTripFields(editing: saved)
        #expect(SavedTripPatch(from: saved, to: try fields.validated.get()) == nil)
        fields.name = "Bank run"
        fields.details.endLocation = ""
        #expect(SavedTripPatch(from: saved, to: try fields.validated.get()) == SavedTripPatch(name: "Bank run", endLocation: .some(nil)))
    }

    @Test func odometerFieldsNeedAStartAndAnEndNoLowerThanIt() {
        #expect(OdometerFields(startText: "", endText: "").validated == .failure(.startReading))
        #expect(OdometerFields(startText: "41250.55").validated == .failure(.startReading))
        #expect(OdometerFields(startText: "41250", endText: "abc").validated == .failure(.endReading))
        #expect(OdometerFields(startText: "41250", endText: "41249.9").validated == .failure(.endBeforeStart))
        #expect(OdometerFields(startText: "0", endText: " ").validated == .success(OdometerInput(startReading: Miles(0), endReading: nil)))
        #expect(OdometerFields(startText: "41,250", endText: "41250").validated
            == .success(OdometerInput(startReading: Miles(41250), endReading: Miles(41250))))
    }

    @Test func rateFieldsNeedARateAboveZeroWithFourDecimals() throws {
        #expect(MileageRateFields(rateText: "").validated == .failure(.rate))
        #expect(MileageRateFields(rateText: "0").validated == .failure(.rate))
        #expect(MileageRateFields(rateText: "0.72501").validated == .failure(.rate))
        #expect(try MileageRateFields(rateText: "0.725").validated.get().rate == PerMileRate(Decimal(string: "0.725")!))
    }

    @Test func rateShowsItsStartDateAndCentsPerMile() throws {
        let rate = try JSONDecoder.api.decode(
            MileageRate.self, from: Data(rateJSON(id: UUID(), from: "2026-07-01", rate: "0.7600").utf8)
        )
        #expect(rate.label == "From Jul 1, 2026 · 76¢/mi")
    }

    // MARK: Saved trips

    @Test func logAddsTheTripToTheListAndOffersItsUndo() async throws {
        defer { try? fixture.cleanUp() }
        let today = CalendarDate(date: .now)
        let (logged, older) = (UUID(), UUID())
        let (model, api) = await loadedMileageModel(
            [tripJSON(id: older, date: "\(today.year)-01-01", createdAt: "2000-01-01T00:00:00Z")], year: today.year
        )
        let saved = try decodedSavedTrip(savedTripJSON(id: UUID()))
        var requests = reply(201, body: Data(tripJSON(id: logged, date: today.apiString).utf8)).makeAsyncIterator()

        await model.log(saved, api: api)
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "POST")
        #expect(request.url?.path() == "/api/mileage/saved-trips/\(uuidString(saved.id))/log")
        let sentId = try #require(try mileageRequestJSON(request)["id"] as? String)
        #expect(UUID(uuidString: sentId) != nil)
        #expect(model.lastLogged?.id == logged)
        #expect(model.lastLogged?.name == "Office run")
        #expect(model.trips.map(\.id) == [logged, older])
        #expect(model.failedLog == nil)

        model.expireUndo(id: UUID())
        #expect(model.lastLogged != nil)
        model.expireUndo(id: logged)
        #expect(model.lastLogged == nil)
    }

    /// A model whose year is this one, after `saved` was logged as trip `logged` beside the trip `older`.
    private func loggedMileageModel(
        saved: SavedTrip, logged: UUID, older: UUID
    ) async -> (MileageModel, APIClient) {
        let year = CalendarDate(date: .now).year
        let (model, api) = await loadedMileageModel([tripJSON(id: older, date: "\(year)-01-01")], year: year)
        _ = reply(201, body: Data(tripJSON(id: logged, date: CalendarDate(date: .now).apiString).utf8))
        await model.log(saved, api: api)
        return (model, api)
    }

    @Test func undoWithinTheWindowDeletesTheLoggedTrip() async throws {
        defer { try? fixture.cleanUp() }
        let (logged, older) = (UUID(), UUID())
        let saved = try decodedSavedTrip(savedTripJSON(id: UUID()))
        let (model, api) = await loggedMileageModel(saved: saved, logged: logged, older: older)
        #expect(model.trips.map(\.id) == [logged, older])
        var requests = reply(204).makeAsyncIterator()

        await model.undoLog(api: api)
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "DELETE")
        #expect(request.url?.path() == "/api/mileage/trips/\(uuidString(logged))")
        #expect(bodyData(request) == nil)
        #expect(model.trips.map(\.id) == [older])
        #expect(model.lastLogged == nil)
        #expect(model.editError == nil)
    }

    @Test func undoWithAReloadInFlightEndsOnTheServersList() async throws {
        defer { try? fixture.cleanUp() }
        let (logged, older) = (UUID(), UUID())
        let year = CalendarDate(date: .now).year
        let saved = try decodedSavedTrip(savedTripJSON(id: UUID()))
        let (model, api) = await loggedMileageModel(saved: saved, logged: logged, older: older)
        let olderJSON = tripJSON(id: older, date: "\(year)-01-01")
        let stale = jsonArray([tripJSON(id: logged, date: CalendarDate(date: .now).apiString), olderJSON])
        let held = holdLoads(page: stale, reloaded: jsonArray([olderJSON]), other: { _ in (204, "") })

        let refresh = Task { await model.load(year: year, api: api) }
        for await _ in held.arrived { break }
        await model.undoLog(api: api)
        held.release.yield()
        await refresh.value

        #expect(model.trips.map(\.id) == [older])
        #expect(model.lastLogged == nil)
        #expect(model.editError == nil)
    }

    @Test func undoOfAnUnlistedTripSendsTheDeleteAndShowsItsFailure() async throws {
        defer { try? fixture.cleanUp() }
        let logged = UUID()
        let (model, api) = await loadedMileageModel([], year: 2000)
        _ = reply(201, body: Data(tripJSON(id: logged, date: CalendarDate(date: .now).apiString).utf8))
        await model.log(try decodedSavedTrip(savedTripJSON(id: UUID())), api: api)
        #expect(model.trips.isEmpty)
        #expect(model.lastLogged?.id == logged)

        var requests = reply(500, body: errorJSON("internal", "Down.")).makeAsyncIterator()
        await model.undoLog(api: api)
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "DELETE")
        #expect(request.url?.path() == "/api/mileage/trips/\(uuidString(logged))")
        #expect(model.editError?.message == "Client meeting: Down.")
        model.clearEditError()
        #expect(model.editError == nil)
    }

    @Test func retryingAFailedLogReusesItsTripIdAndANewTapDoesNot() async throws {
        defer { try? fixture.cleanUp() }
        let today = CalendarDate(date: .now)
        let (model, api) = await loadedMileageModel([], year: today.year)
        let saved = try decodedSavedTrip(savedTripJSON(id: UUID()))
        let loggedId = UUID()
        let loggedBody = Data(tripJSON(id: loggedId, date: today.apiString).utf8)
        var requests = replyInTurn([
            (500, errorJSON("internal", "Down.")), (201, loggedBody), (201, loggedBody),
        ]).makeAsyncIterator()

        await model.log(saved, api: api)
        let failed = try #require(model.failedLog)
        #expect(failed.error.localizedDescription == "Down.")
        #expect(failed.saved == saved)
        #expect(model.trips.isEmpty)
        #expect(model.lastLogged == nil)

        await model.retryLog(api: api)
        await model.log(saved, api: api)
        var sentIds: [String?] = []
        for _ in 0..<3 {
            sentIds.append(try mileageRequestJSON(try #require(await requests.next()))["id"] as? String)
        }
        #expect(sentIds[0] == failed.tripId.uuidString)
        #expect(sentIds[1] == failed.tripId.uuidString)
        #expect(sentIds[2] != failed.tripId.uuidString)
        #expect(model.failedLog == nil)
        #expect(model.trips.map(\.id) == [loggedId])
    }

    @Test func aSecondTapWhileASavedTripLogsSendsNothing() async throws {
        defer { try? fixture.cleanUp() }
        let today = CalendarDate(date: .now)
        let (model, api) = await loadedMileageModel([], year: today.year)
        let saved = try decodedSavedTrip(savedTripJSON(id: UUID()))
        let firstLogged = UUID()
        let firstBody = Data(tripJSON(id: firstLogged, date: today.apiString).utf8)
        let secondBody = Data(tripJSON(id: UUID(), date: today.apiString).utf8)
        let (arrived, arrivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let paths = OSAllocatedUnfairLock(initialState: [String]())
        StubURLProtocol.respond { request in
            let count = paths.withLock { paths in
                paths.append("\(request.httpMethod ?? "") \(request.url?.path() ?? "")")
                return paths.count
            }
            guard count == 1 else { return (StubURLProtocol.response(to: request, status: 201), secondBody) }
            arrivedSignal.yield()
            for await _ in release { break }
            return (StubURLProtocol.response(to: request, status: 201), firstBody)
        }

        let first = Task { await model.log(saved, api: api) }
        for await _ in arrived { break }
        #expect(model.loggingIds == [saved.id])
        await model.log(saved, api: api)
        releaseSignal.yield()
        await first.value

        #expect(paths.withLock { $0 } == ["POST /api/mileage/saved-trips/\(uuidString(saved.id))/log"])
        #expect(model.trips.map(\.id) == [firstLogged])
        #expect(model.lastLogged?.id == firstLogged)
        #expect(model.loggingIds.isEmpty)
    }

    private func loadedSavedTripsModel(_ rows: [String]) async -> (SavedTripsModel, APIClient) {
        let api = makeClient(fixture.make())
        let model = SavedTripsModel()
        _ = reply(200, body: jsonArray(rows))
        await model.load(api: api)
        return (model, api)
    }

    @Test func savedTripCreateIsListedByName() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api) = await loadedSavedTripsModel([
            savedTripJSON(id: UUID(), name: "Bank"), savedTripJSON(id: UUID(), name: "Office"),
        ])
        let form = try SavedTripFields(name: "Client", details: TripDetailFields(milesText: "20", purpose: "Visit"))
            .validated.get()
        let draft = SavedTripDraft(id: UUID(), form: form)
        var created = reply(201, body: Data(savedTripJSON(id: draft.id, name: "Client").utf8)).makeAsyncIterator()

        #expect(await model.create(draft, api: api))
        let post = try #require(await created.next())
        #expect(Set(try mileageRequestJSON(post).keys) == ["id", "name", "miles", "purpose"])
        #expect(model.savedTrips.map(\.name) == ["Bank", "Client", "Office"])
    }

    @Test func savedTripRenameResorts() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<2).map { _ in UUID() }
        let (model, api) = await loadedSavedTripsModel([
            savedTripJSON(id: ids[0], name: "Bank"), savedTripJSON(id: ids[1], name: "Office"),
        ])
        _ = reply(200, body: Data(savedTripJSON(id: ids[0], name: "Zoo").utf8))

        await model.update(model.savedTrips[0], patch: SavedTripPatch(name: "Zoo"), api: api)
        #expect(model.savedTrips.map(\.name) == ["Office", "Zoo"])
        #expect(model.savedTrips.map(\.id) == [ids[1], ids[0]])
    }

    @Test func savedTripDeleteLeavesTheList() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<3).map { _ in UUID() }
        let (model, api) = await loadedSavedTripsModel([
            savedTripJSON(id: ids[0], name: "Bank"), savedTripJSON(id: ids[1], name: "Client"),
            savedTripJSON(id: ids[2], name: "Office"),
        ])
        var deleted = reply(204).makeAsyncIterator()

        await model.delete(model.savedTrips[0], api: api)
        let request = try #require(await deleted.next())
        #expect(request.httpMethod == "DELETE")
        #expect(request.url?.path() == "/api/mileage/saved-trips/\(uuidString(ids[0]))")
        #expect(model.savedTrips.map(\.id) == [ids[1], ids[2]])
    }

    // MARK: Odometer and rates

    @Test func odometerLoadsNothingForANewYearThenSavesTheReading() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = OdometerModel()
        _ = reply(404, body: errorJSON("not_found", "Not found."))
        await model.load(year: 2026, api: api)
        #expect(model.hasData)
        #expect(model.reading == nil)

        let input = try OdometerFields(startText: "41250", endText: "").validated.get()
        let saved = #"{"year":2026,"start_reading":"41250.0","end_reading":null,"updated_at":"2026-10-20T01:27:40Z"}"#
        var requests = reply(200, body: Data(saved.utf8)).makeAsyncIterator()
        #expect(await model.save(input, year: 2026, api: api))
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "PUT")
        #expect(request.url?.path() == "/api/mileage/odometer/2026")
        let json = try mileageRequestJSON(request)
        #expect(json["start_reading"] as? String == "41250.0")
        #expect(json["end_reading"] is NSNull)
        #expect(model.reading?.startReading == Miles(41250))
        #expect(model.saveError == nil)
        #expect(OdometerFields(editing: model.reading) == OdometerFields(startText: "41250.0", endText: ""))

        _ = reply(422, body: errorJSON("validation_error", "end_reading must be at least start_reading"))
        #expect(!(await model.save(input, year: 2026, api: api)))
        #expect(model.saveError?.localizedDescription == "end_reading must be at least start_reading")
        #expect(model.reading?.startReading == Miles(41250))
    }

    @Test func ratesListOldestFirstAndADuplicateDateShowsTheServersMessage() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = (0..<3).map { _ in UUID() }
        let model = MileageRatesModel()
        _ = reply(200, body: jsonArray([
            rateJSON(id: ids[0], from: "2026-01-01", rate: "0.7250"),
            rateJSON(id: ids[1], from: "2026-07-01", rate: "0.7600"),
        ]))
        await model.load(api: api)
        #expect(model.rates.map(\.id) == [ids[0], ids[1]])

        let message = "A rate already starts on this date."
        let duplicate = MileageRateDraft(id: ids[2], effectiveFrom: CalendarDate(year: 2026, month: 7, day: 1), rate: PerMileRate(1))
        var requests = reply(409, body: errorJSON("conflict", message)).makeAsyncIterator()
        #expect(!(await model.create(duplicate, api: api)))
        let request = try #require(await requests.next())
        #expect(request.url?.path() == "/api/mileage/rates")
        #expect(try mileageRequestJSON(request)["rate"] as? String == "1.0000")
        #expect(model.createError?.status == 409)
        #expect(model.createError?.localizedDescription == message)
        #expect(model.rates.map(\.id) == [ids[0], ids[1]])

        let earlier = MileageRateDraft(id: ids[2], effectiveFrom: CalendarDate(year: 2025, month: 1, day: 1), rate: PerMileRate(Decimal(string: "0.7")!))
        _ = reply(201, body: Data(rateJSON(id: ids[2], from: "2025-01-01", rate: "0.7000").utf8))
        #expect(await model.create(earlier, api: api))
        #expect(model.createError == nil)
        #expect(model.rates.map(\.id) == [ids[2], ids[0], ids[1]])

        _ = reply(409, body: errorJSON("conflict", message))
        await model.update(model.rates[1], patch: MileageRatePatch(effectiveFrom: CalendarDate(year: 2026, month: 7, day: 1)), api: api)
        #expect(model.rates.map(\.id) == [ids[2], ids[0], ids[1]])
        #expect(model.editError?.message == "From Jan 1, 2026 · 72.5¢/mi: \(message)")

        _ = reply(204)
        await model.delete(model.rates[0], api: api)
        #expect(model.rates.map(\.id) == [ids[0], ids[1]])
    }
}
