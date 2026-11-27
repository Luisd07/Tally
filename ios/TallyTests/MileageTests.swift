import Foundation
import Testing
@testable import Tally

private let tripId = UUID(uuidString: "0b5c6a10-1111-4a2b-9c3d-000000000002")!
private let savedTripId = UUID(uuidString: "0b5c6a10-2222-4a2b-9c3d-000000000001")!
private let rateId = UUID(uuidString: "0b5c6a10-3333-4a2b-9c3d-000000000001")!

extension APIClientTests {
    private func miles(_ text: String) -> Miles {
        Miles(Decimal(string: text)!)
    }

    private func rate(_ text: String) -> PerMileRate {
        PerMileRate(Decimal(string: text)!)
    }

    /// The JSON object a mileage request carried.
    private func mileageBody(_ request: URLRequest) throws -> [String: Any] {
        let body = try #require(bodyData(request))
        return try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
    }

    /// Answers the next request with `status` and the fixture `fixture` (no body when nil), runs `call`, and
    /// returns the request it sent with what `call` returned.
    private func sent<T>(
        _ status: Int = 200, fixture name: String? = nil, _ call: (APIClient) async throws -> T
    ) async throws -> (request: URLRequest, result: T) {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        var requests = reply(status, body: try name.map(jsonFixture) ?? Data()).makeAsyncIterator()
        let result = try await call(api)
        return (try #require(await requests.next()), result)
    }

    // MARK: Models

    @Test func tripsFixtureDecodesNewestFirst() throws {
        let trips = try JSONDecoder.api.decode([Trip].self, from: jsonFixture("mileage-trips-2026"))
        #expect(trips.map(\.miles) == [miles("8.2"), miles("41.5"), miles("12.5")])
        #expect(trips.map(\.date) == trips.map(\.date).sorted(by: >))
        #expect(trips[1].id == tripId)
        #expect(trips[1].startLocation == nil)
        #expect(trips[1].endLocation == "Warehouse")
        #expect(trips[2].purpose == "Client meeting at Acme")
    }

    @Test func savedTripsOdometerAndRatesFixturesDecode() throws {
        let saved = try JSONDecoder.api.decode([SavedTrip].self, from: jsonFixture("mileage-saved-trips"))
        #expect(saved.map(\.name) == ["Office run"])
        #expect(saved.first?.miles == miles("8.2"))

        let odometer = try JSONDecoder.api.decode(OdometerReading.self, from: jsonFixture("mileage-odometer-2026"))
        #expect(odometer.year == 2026)
        #expect(odometer.startReading == miles("41250.0"))
        #expect(odometer.totalMiles == miles("12730.4"))

        let rates = try JSONDecoder.api.decode([MileageRate].self, from: jsonFixture("mileage-rates"))
        #expect(rates.map(\.effectiveFrom) == rates.map(\.effectiveFrom).sorted())
        #expect(rates.map(\.rate) == [rate("0.7"), rate("0.725"), rate("0.76"), rate("0.735")])
    }

    @Test func aPatchedRateEchoedAtItsSentScaleDecodes() throws {
        let patched = try JSONDecoder.api.decode(MileageRate.self, from: jsonFixture("mileage-rate-patched"))
        #expect(patched.id == rateId)
        #expect(patched.rate == rate("0.7350"))
        #expect(patched.rate.inputText == "0.7350")
    }

    @Test func odometerWithoutAnEndHasNoTotal() throws {
        let json = #"{"year":2026,"start_reading":"41250.0","end_reading":null,"updated_at":"2026-10-20T01:27:40Z"}"#
        let reading = try JSONDecoder.api.decode(OdometerReading.self, from: Data(json.utf8))
        #expect(reading.totalMiles == nil)
    }

    @Test func milesAndRatesEncodeAtTheServersScale() throws {
        #expect(miles("40").inputText == "40.0")
        #expect(rate("0.725").inputText == "0.7250")
        let body = try snakeCaseKeys(OdometerInput(startReading: miles("41250"), endReading: miles("53980.4")))
        #expect(body["start_reading"] as? String == "41250.0")
        #expect(body["end_reading"] as? String == "53980.4")
    }

    @Test func rateShowsAsDollarsOrCentsPerMile() {
        #expect(rate("0.7250").perMileText == "$0.725/mi")
        #expect(rate("0.7").perMileText == "$0.70/mi")
        #expect(rate("0.7250").centsText == "72.5¢")
        #expect(rate("0.70").centsText == "70¢")
        #expect(miles("12730.4").formatted() == "12,730.4 mi")
        #expect(miles("40.0").formatted() == "40 mi")
    }

    @Test func typedDecimalsTakeAtMostTheirScale() {
        #expect(Miles(userInput: " 12.5 ") == miles("12.5"))
        #expect(Miles(userInput: "41,250") == miles("41250"))
        #expect(Miles(userInput: "12.55") == nil)
        #expect(Miles(userInput: "-1") == nil)
        #expect(Miles(userInput: "4,12") == nil)
        #expect(PerMileRate(userInput: "0.7250") == rate("0.725"))
        #expect(PerMileRate(userInput: ".725") == rate("0.725"))
        #expect(PerMileRate(userInput: "0.72501") == nil)
    }

    // MARK: Trips

    @Test func tripsSendsTheYear() async throws {
        let (request, trips) = try await sent(fixture: "mileage-trips-2026") { try await $0.trips(year: 2026) }
        #expect(request.httpMethod == "GET")
        #expect(request.url?.path() == "/api/mileage/trips")
        #expect(queryItems(request) == [URLQueryItem(name: "year", value: "2026")])
        #expect(trips.count == 3)

        let (all, _) = try await sent(fixture: "mileage-trips-2026") { try await $0.trips() }
        #expect(queryItems(all).isEmpty)
    }

    @Test func createTripPostsTheDraftWithItsId() async throws {
        let draft = TripDraft(
            id: tripId, date: CalendarDate(year: 2026, month: 8, day: 14), miles: miles("40"), purpose: "Site visit",
            startLocation: "Home"
        )
        let (request, trip) = try await sent(201, fixture: "mileage-trip-patched") { try await $0.createTrip(draft) }
        #expect(request.httpMethod == "POST")
        #expect(request.url?.path() == "/api/mileage/trips")
        let body = try mileageBody(request)
        #expect(body["id"] as? String == tripId.uuidString)
        #expect(body["date"] as? String == "2026-08-14")
        #expect(body["miles"] as? String == "40.0")
        #expect(body["purpose"] as? String == "Site visit")
        #expect(body["start_location"] as? String == "Home")
        #expect(body["end_location"] == nil)
        #expect(trip.id == tripId)
    }

    @Test func updateTripSendsOnlyTheSetFields() async throws {
        let patch = TripPatch(miles: miles("41.5"), startLocation: .some(nil), endLocation: "Warehouse")
        let (request, trip) = try await sent(fixture: "mileage-trip-patched") {
            try await $0.updateTrip(id: tripId, patch: patch)
        }
        #expect(request.httpMethod == "PATCH")
        #expect(request.url?.path() == "/api/mileage/trips/0b5c6a10-1111-4a2b-9c3d-000000000002")
        let body = try mileageBody(request)
        #expect(Set(body.keys) == ["miles", "start_location", "end_location"])
        #expect(body["miles"] as? String == "41.5")
        #expect(body["start_location"] is NSNull)
        #expect(body["end_location"] as? String == "Warehouse")
        #expect(trip.miles == miles("41.5"))
    }

    @Test func deleteTripSendsNoBody() async throws {
        let (request, _) = try await sent(204) { try await $0.deleteTrip(id: tripId) }
        #expect(request.httpMethod == "DELETE")
        #expect(request.url?.path() == "/api/mileage/trips/0b5c6a10-1111-4a2b-9c3d-000000000002")
        #expect(bodyData(request) == nil)
    }

    // MARK: Saved trips

    @Test func savedTripsAreListed() async throws {
        let (request, saved) = try await sent(fixture: "mileage-saved-trips") { try await $0.savedTrips() }
        #expect(request.httpMethod == "GET")
        #expect(request.url?.path() == "/api/mileage/saved-trips")
        #expect(queryItems(request).isEmpty)
        #expect(saved.first?.id == savedTripId)
    }

    @Test func createSavedTripPostsTheDraft() async throws {
        let draft = SavedTripDraft(
            id: savedTripId, name: "Office run", miles: miles("8.2"), purpose: "Drop off documents",
            startLocation: "Home", endLocation: "Office"
        )
        let (request, saved) = try await sent(201, fixture: "mileage-saved-trip") { try await $0.createSavedTrip(draft) }
        #expect(request.httpMethod == "POST")
        #expect(request.url?.path() == "/api/mileage/saved-trips")
        let body = try mileageBody(request)
        #expect(body["id"] as? String == savedTripId.uuidString)
        #expect(body["name"] as? String == "Office run")
        #expect(body["miles"] as? String == "8.2")
        #expect(body["purpose"] as? String == "Drop off documents")
        #expect(body["start_location"] as? String == "Home")
        #expect(body["end_location"] as? String == "Office")
        #expect(saved.id == savedTripId)
    }

    @Test func updateSavedTripSendsOnlyTheSetFields() async throws {
        let patch = SavedTripPatch(name: "Office", endLocation: .some(nil))
        let (request, _) = try await sent(fixture: "mileage-saved-trip") {
            try await $0.updateSavedTrip(id: savedTripId, patch: patch)
        }
        #expect(request.httpMethod == "PATCH")
        #expect(request.url?.path() == "/api/mileage/saved-trips/0b5c6a10-2222-4a2b-9c3d-000000000001")
        let body = try mileageBody(request)
        #expect(Set(body.keys) == ["name", "end_location"])
        #expect(body["name"] as? String == "Office")
        #expect(body["end_location"] is NSNull)
    }

    @Test func deleteSavedTripSendsNoBody() async throws {
        let (request, _) = try await sent(204) { try await $0.deleteSavedTrip(id: savedTripId) }
        #expect(request.httpMethod == "DELETE")
        #expect(request.url?.path() == "/api/mileage/saved-trips/0b5c6a10-2222-4a2b-9c3d-000000000001")
        #expect(bodyData(request) == nil)
    }

    @Test func logSavedTripSendsTheNewTripsId() async throws {
        let newTrip = UUID(uuidString: "0b5c6a10-1111-4a2b-9c3d-000000000003")!
        let (request, trip) = try await sent(201, fixture: "mileage-trip-logged") {
            try await $0.logSavedTrip(id: savedTripId, tripId: newTrip)
        }
        #expect(request.httpMethod == "POST")
        #expect(request.url?.path() == "/api/mileage/saved-trips/0b5c6a10-2222-4a2b-9c3d-000000000001/log")
        let body = try mileageBody(request)
        #expect(Set(body.keys) == ["id"])
        #expect(body["id"] as? String == newTrip.uuidString)
        #expect(trip.id == newTrip)
        #expect(trip.miles == miles("8.2"))
    }

    // MARK: Odometer

    @Test func odometerGetsTheYear() async throws {
        let (request, reading) = try await sent(fixture: "mileage-odometer-2026") { try await $0.odometer(year: 2026) }
        #expect(request.httpMethod == "GET")
        #expect(request.url?.path() == "/api/mileage/odometer/2026")
        #expect(reading?.endReading == miles("53980.4"))
    }

    @Test func odometerIsNilWhenTheYearHasNone() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        _ = reply(404, body: Data(#"{"error":{"code":"not_found","message":"Not found.","request_id":"r1"}}"#.utf8))
        #expect(try await api.odometer(year: 2025) == nil)
    }

    @Test func saveOdometerPutsTheWholeReading() async throws {
        let (request, reading) = try await sent(fixture: "mileage-odometer-2026") {
            try await $0.saveOdometer(year: 2026, OdometerInput(startReading: miles("41250"), endReading: nil))
        }
        #expect(request.httpMethod == "PUT")
        #expect(request.url?.path() == "/api/mileage/odometer/2026")
        let body = try mileageBody(request)
        #expect(Set(body.keys) == ["start_reading", "end_reading"])
        #expect(body["start_reading"] as? String == "41250.0")
        #expect(body["end_reading"] is NSNull)
        #expect(reading.year == 2026)
    }

    // MARK: Rates

    @Test func mileageRatesAreListed() async throws {
        let (request, rates) = try await sent(fixture: "mileage-rates") { try await $0.mileageRates() }
        #expect(request.httpMethod == "GET")
        #expect(request.url?.path() == "/api/mileage/rates")
        #expect(rates.count == 4)
    }

    @Test func createMileageRatePostsTheDraft() async throws {
        let draft = MileageRateDraft(id: rateId, effectiveFrom: CalendarDate(year: 2027, month: 1, day: 1), rate: rate("0.73"))
        let (request, created) = try await sent(201, fixture: "mileage-rate-created") {
            try await $0.createMileageRate(draft)
        }
        #expect(request.httpMethod == "POST")
        #expect(request.url?.path() == "/api/mileage/rates")
        let body = try mileageBody(request)
        #expect(Set(body.keys) == ["id", "effective_from", "rate"])
        #expect(body["id"] as? String == rateId.uuidString)
        #expect(body["effective_from"] as? String == "2027-01-01")
        #expect(body["rate"] as? String == "0.7300")
        #expect(created.rate == rate("0.73"))
    }

    @Test func updateMileageRateSendsOnlyTheSetFields() async throws {
        let (request, updated) = try await sent(fixture: "mileage-rate-patched") {
            try await $0.updateMileageRate(id: rateId, patch: MileageRatePatch(rate: rate("0.735")))
        }
        #expect(request.httpMethod == "PATCH")
        #expect(request.url?.path() == "/api/mileage/rates/0b5c6a10-3333-4a2b-9c3d-000000000001")
        let body = try mileageBody(request)
        #expect(Set(body.keys) == ["rate"])
        #expect(body["rate"] as? String == "0.7350")
        #expect(updated.rate == rate("0.735"))
    }

    @Test func deleteMileageRateSendsNoBody() async throws {
        let (request, _) = try await sent(204) { try await $0.deleteMileageRate(id: rateId) }
        #expect(request.httpMethod == "DELETE")
        #expect(request.url?.path() == "/api/mileage/rates/0b5c6a10-3333-4a2b-9c3d-000000000001")
        #expect(bodyData(request) == nil)
    }
}
