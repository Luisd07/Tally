import Foundation

/// A decimal the API sends and accepts as a JSON string, like `Money` but at its own scale: miles and odometer
/// readings at one decimal place, a per-mile rate at four. Decoding takes any decimal string (a PATCH may echo
/// `"0.73"` back as sent); encoding always writes `fractionDigits` decimals.
protocol DecimalString: Codable, Hashable, Comparable {
    /// The decimals the server stores, and the most `init(userInput:)` accepts.
    static var fractionDigits: Int { get }
    /// What `init(userInput:)` accepts, built once by `decimalInputRegex(fractionDigits:)`.
    static var inputRegex: Regex<AnyRegexOutput> { get }
    var value: Decimal { get }
    init(_ value: Decimal)
}

extension DecimalString {
    init(from decoder: Decoder) throws {
        let string = try decoder.singleValueContainer().decode(String.self)
        guard let value = Decimal(string: string, locale: decimalPOSIX) else {
            throw DecodingError.dataCorrupted(
                .init(codingPath: decoder.codingPath, debugDescription: "Invalid decimal \(string)")
            )
        }
        self.init(value)
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(inputText)
    }

    /// What the user typed: digits, optionally grouped with commas in the right places ("41,250"), then an
    /// optional "." and at most `fractionDigits` decimals. Surrounding whitespace is ignored. Anything else (a
    /// sign, letters, one decimal too many) is nil, never a rounded number.
    init?(userInput text: String) {
        guard let trimmed = text.trimmedOrNil,
              trimmed.wholeMatch(of: Self.inputRegex) != nil,
              let value = Decimal(string: trimmed.replacing(",", with: ""), locale: decimalPOSIX)
        else { return nil }
        self.init(value)
    }

    /// `fractionDigits` decimals, no grouping, "." as the separator: what the server takes and
    /// `init(userInput:)` reads.
    var inputText: String {
        value.formatted(
            Decimal.FormatStyle(locale: decimalPOSIX).grouping(.never).precision(.fractionLength(Self.fractionDigits))
        )
    }

    static func < (lhs: Self, rhs: Self) -> Bool { lhs.value < rhs.value }
}

private let decimalPOSIX = Locale(identifier: "en_US_POSIX")

/// `DecimalString.init(userInput:)`'s pattern for `digits` decimals. It is fixed apart from `digits`, so a pattern
/// that doesn't build is a bug, not bad input.
private func decimalInputRegex(fractionDigits digits: Int) -> Regex<AnyRegexOutput> {
    try! Regex(#"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{0,\#(digits)})?|\.\d{1,\#(digits)}"#)
}

/// A distance or an odometer reading in miles, to one decimal place.
struct Miles: @MainActor DecimalString {
    static var fractionDigits: Int { 1 }
    static let inputRegex = decimalInputRegex(fractionDigits: fractionDigits)
    let value: Decimal

    init(_ value: Decimal) {
        self.value = value
    }

    /// "12.5 mi", "40 mi" or "12,730.4 mi".
    func formatted() -> String {
        "\(value.formatted(.number.precision(.fractionLength(0...1)))) mi"
    }
}

/// The IRS standard mileage rate, in dollars per mile, to four decimal places.
struct PerMileRate: @MainActor DecimalString {
    static var fractionDigits: Int { 4 }
    static let inputRegex = decimalInputRegex(fractionDigits: fractionDigits)
    let value: Decimal

    init(_ value: Decimal) {
        self.value = value
    }

    /// Dollars per mile with two to four decimals: "$0.725/mi", "$0.70/mi".
    var perMileText: String {
        "\(value.formatted(.currency(code: "USD").precision(.fractionLength(2...4))))/mi"
    }

    /// Cents per mile with up to two decimals: "72.5¢", "70¢".
    var centsText: String {
        "\((value * 100).formatted(.number.precision(.fractionLength(0...2))))¢"
    }
}

/// Mirrors `TripOut`: one business trip in the mileage log.
struct Trip: Decodable, Identifiable, Hashable {
    let id: UUID
    let date: CalendarDate
    let miles: Miles
    let purpose: String
    let startLocation: String?
    let endLocation: String?
    let createdAt: Date
    let updatedAt: Date
}

/// Mirrors `SavedTripOut`: a trip made often, logged again with one tap.
struct SavedTrip: Decodable, Identifiable, Hashable {
    let id: UUID
    let name: String
    let miles: Miles
    let purpose: String
    let startLocation: String?
    let endLocation: String?
    let createdAt: Date
    let updatedAt: Date
}

/// Mirrors `OdometerOut`: the year's start and end readings.
struct OdometerReading: Decodable, Hashable {
    let year: Int
    let startReading: Miles
    let endReading: Miles?
    let updatedAt: Date

    /// Miles driven in the year, once the end reading is set.
    var totalMiles: Miles? { milesDriven(from: startReading, to: endReading) }
}

/// Miles driven from odometer reading `start` to `end`; nil until the end reading is set.
private func milesDriven(from start: Miles, to end: Miles?) -> Miles? {
    end.map { Miles($0.value - start.value) }
}

/// Mirrors `RateOut`: a per-mile rate in force from `effectiveFrom` until the next rate starts.
struct MileageRate: Decodable, Identifiable, Hashable {
    let id: UUID
    let effectiveFrom: CalendarDate
    let rate: PerMileRate
    let createdAt: Date
    let updatedAt: Date
}

/// Mirrors `MileageRateUsed`: a rate the year's trips were priced at, and the business miles priced at it.
struct MileageRateUsed: Decodable, Hashable {
    let effectiveFrom: CalendarDate
    let rate: PerMileRate
    let miles: Miles
}

/// Mirrors `MileageSummary`: how line 9 (`car_truck`) was built under the standard mileage rate. The line's
/// spend is `mileageAmount + parkingTolls`. The odometer fields are nil until the year's reading is set, and
/// `totalMiles` also needs the end reading.
struct MileageSummary: Decodable, Equatable {
    let businessMiles: Miles
    let mileageAmount: Money
    let parkingTolls: Money
    /// Miles on dates with no rate in force; they add nothing.
    let unpricedMiles: Miles
    /// Gas, repairs and insurance: replaced by the standard mileage rate, so deducted nowhere.
    let excludedActualCarCosts: Money
    let rates: [MileageRateUsed]
    let odometerStart: Miles?
    let odometerEnd: Miles?
    let totalMiles: Miles?

    /// Whether there is anything to show: logged miles or actual car costs.
    var hasActivity: Bool {
        businessMiles.value > 0 || excludedActualCarCosts.amount != 0
    }
}

/// Body for `POST /api/mileage/trips`. Unset locations are left out. `id` is made on the phone, so retrying
/// the same draft never logs a second trip.
struct TripDraft: Encodable {
    var id = UUID()
    var date: CalendarDate
    var miles: Miles
    var purpose: String
    var startLocation: String?
    var endLocation: String?
}

/// The miles, purpose and route a trip or saved-trip PATCH sets. Only fields that were set are sent. The locations
/// take `.some(nil)` to clear; `miles` and `purpose` are single-optional because the server rejects null.
struct TripDetailsPatch: Equatable {
    var miles: Miles?
    var purpose: String?
    var startLocation: String??
    var endLocation: String??
}

/// The coding keys of a PATCH body that carries `TripDetailsPatch`'s fields.
nonisolated private protocol TripDetailsCodingKey: CodingKey {
    static var miles: Self { get }
    static var purpose: Self { get }
    static var startLocation: Self { get }
    static var endLocation: Self { get }
}

private extension KeyedEncodingContainer where Key: TripDetailsCodingKey {
    /// Writes the fields `details` sets.
    mutating func encodeDetails(_ details: TripDetailsPatch) throws {
        if let miles = details.miles { try encode(miles, forKey: .miles) }
        if let purpose = details.purpose { try encode(purpose, forKey: .purpose) }
        try encodePatchField(details.startLocation, forKey: .startLocation)
        try encodePatchField(details.endLocation, forKey: .endLocation)
    }
}

/// Body for `PATCH /api/mileage/trips/{id}`: `details`' fields, and `date` when set (never null).
struct TripPatch: Encodable, Equatable {
    var date: CalendarDate?
    var details: TripDetailsPatch

    private enum CodingKeys: String, TripDetailsCodingKey {
        case date, miles, purpose, startLocation, endLocation
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        if let date { try container.encode(date, forKey: .date) }
        try container.encodeDetails(details)
    }
}

extension TripPatch {
    /// A patch that sets the fields given and leaves the rest out.
    init(
        date: CalendarDate? = nil, miles: Miles? = nil, purpose: String? = nil, startLocation: String?? = nil,
        endLocation: String?? = nil
    ) {
        self.init(
            date: date,
            details: TripDetailsPatch(miles: miles, purpose: purpose, startLocation: startLocation, endLocation: endLocation)
        )
    }
}

/// Body for `POST /api/mileage/saved-trips`. Unset locations are left out; `id` is made on the phone.
struct SavedTripDraft: Encodable {
    var id = UUID()
    var name: String
    var miles: Miles
    var purpose: String
    var startLocation: String?
    var endLocation: String?
}

/// Body for `PATCH /api/mileage/saved-trips/{id}`: `details`' fields, and `name` when set (never null).
struct SavedTripPatch: Encodable, Equatable {
    var name: String?
    var details: TripDetailsPatch

    private enum CodingKeys: String, TripDetailsCodingKey {
        case name, miles, purpose, startLocation, endLocation
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        if let name { try container.encode(name, forKey: .name) }
        try container.encodeDetails(details)
    }
}

extension SavedTripPatch {
    /// A patch that sets the fields given and leaves the rest out.
    init(
        name: String? = nil, miles: Miles? = nil, purpose: String? = nil, startLocation: String?? = nil,
        endLocation: String?? = nil
    ) {
        self.init(
            name: name,
            details: TripDetailsPatch(miles: miles, purpose: purpose, startLocation: startLocation, endLocation: endLocation)
        )
    }
}

/// Body for `POST /api/mileage/saved-trips/{id}/log`: the new trip's id, made on the phone, so a retried tap
/// returns the trip it already logged.
struct SavedTripLog: Encodable {
    var id: UUID
}

/// Body for `PUT /api/mileage/odometer/{year}`: the whole reading, replacing what was stored. A missing end
/// reading is sent as null.
struct OdometerInput: Encodable, Equatable {
    var startReading: Miles
    var endReading: Miles?

    private enum CodingKeys: String, CodingKey {
        case startReading, endReading
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(startReading, forKey: .startReading)
        try container.encode(endReading, forKey: .endReading)
    }

    /// Miles driven in the year, once the end reading is set.
    var totalMiles: Miles? { milesDriven(from: startReading, to: endReading) }
}

/// Body for `POST /api/mileage/rates`; `id` is made on the phone. A second rate on the same date is a 409.
struct MileageRateDraft: Encodable {
    var id = UUID()
    var effectiveFrom: CalendarDate
    var rate: PerMileRate
}

/// Body for `PATCH /api/mileage/rates/{id}`. Only fields that were set are sent; neither can be null.
struct MileageRatePatch: Encodable, Equatable {
    var effectiveFrom: CalendarDate?
    var rate: PerMileRate?
}
