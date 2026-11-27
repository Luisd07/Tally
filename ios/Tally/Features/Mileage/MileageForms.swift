import Foundation

/// The rules a mileage sheet's fields can break.
enum MileageFormRule: FormRule, Equatable {
    case name, miles, purpose, location, startReading, endReading, endBeforeStart, rate

    var message: String {
        switch self {
        case .name: "Enter a name of up to \(Self.maxNameLength) characters."
        case .miles: "Enter miles above zero with at most one decimal, like 12.5."
        case .purpose: "Enter the trip's business purpose, up to \(Self.maxTextLength) characters."
        case .location: "A start or end location can't be over \(Self.maxTextLength) characters."
        case .startReading: "Enter the start reading in miles, with at most one decimal."
        case .endReading: "Enter the end reading in miles with at most one decimal, or leave it blank."
        case .endBeforeStart: "The end reading can't be below the start reading."
        case .rate: "Enter a rate above zero with at most four decimals, like 0.725."
        }
    }

    /// The longest saved-trip name the server takes.
    static let maxNameLength = 100
    /// The longest purpose or location the server takes.
    static let maxTextLength = 200
}

/// A trip's miles, purpose and route as entered, before validation; shared by the trip and saved-trip sheets.
struct TripDetailFields: Equatable {
    var milesText = ""
    var purpose = ""
    var startLocation = ""
    var endLocation = ""

    /// The fields of a stored trip or saved trip.
    init(miles: Miles, purpose: String, startLocation: String?, endLocation: String?) {
        self.init(
            milesText: miles.inputText, purpose: purpose, startLocation: startLocation ?? "",
            endLocation: endLocation ?? ""
        )
    }

    init(milesText: String = "", purpose: String = "", startLocation: String = "", endLocation: String = "") {
        self.milesText = milesText
        self.purpose = purpose
        self.startLocation = startLocation
        self.endLocation = endLocation
    }

    /// Miles above zero with at most one decimal, a purpose of 1-`maxTextLength` characters once trimmed, and
    /// locations of at most `maxTextLength` characters (nil when blank); or the first rule they break.
    var validated: Result<TripDetails, MileageFormRule> {
        guard let miles = Miles(userInput: milesText), miles.value > 0 else { return .failure(.miles) }
        guard let purpose = purpose.trimmedOrNil, purpose.count <= MileageFormRule.maxTextLength else { return .failure(.purpose) }
        let start = startLocation.trimmedOrNil
        let end = endLocation.trimmedOrNil
        if [start, end].contains(where: { ($0?.count ?? 0) > MileageFormRule.maxTextLength }) { return .failure(.location) }
        return .success(TripDetails(miles: miles, purpose: purpose, startLocation: start, endLocation: end))
    }
}

/// A trip's miles, purpose and route, validated.
struct TripDetails: Equatable {
    var miles: Miles
    var purpose: String
    var startLocation: String?
    var endLocation: String?
}

/// The trip sheet's fields as entered. A new one is dated today.
struct TripFields: Equatable {
    var date = CalendarDate(date: .now)
    var details = TripDetailFields()

    var validated: Result<TripForm, MileageFormRule> {
        details.validated.map { TripForm(date: date, details: $0) }
    }
}

extension TripFields {
    /// The fields of `trip`, for an edit.
    init(editing trip: Trip) {
        self.init(
            date: trip.date,
            details: TripDetailFields(
                miles: trip.miles, purpose: trip.purpose, startLocation: trip.startLocation,
                endLocation: trip.endLocation
            )
        )
    }
}

/// The trip sheet's fields, validated.
struct TripForm: Equatable {
    var date: CalendarDate
    var details: TripDetails
}

extension TripDraft {
    /// A draft of what `form` holds, under `id` so a retry sends the same one.
    init(id: UUID, form: TripForm) {
        self.init(
            id: id, date: form.date, miles: form.details.miles, purpose: form.details.purpose,
            startLocation: form.details.startLocation, endLocation: form.details.endLocation
        )
    }
}

extension TripDetailsPatch {
    /// The fields where `details` differs from `original`; a location left blank clears it. Text is compared with
    /// the stored value trimmed, so untouched spaces aren't a change.
    init(
        from original: (miles: Miles, purpose: String, startLocation: String?, endLocation: String?),
        to details: TripDetails
    ) {
        self.init(
            miles: details.miles == original.miles ? nil : details.miles,
            purpose: details.purpose == original.purpose.trimmedOrNil ? nil : details.purpose,
            startLocation: details.startLocation == original.startLocation?.trimmedOrNil ? nil : .some(details.startLocation),
            endLocation: details.endLocation == original.endLocation?.trimmedOrNil ? nil : .some(details.endLocation)
        )
    }
}

extension TripPatch {
    /// The fields where `form` differs from `original`, by `TripDetailsPatch.init(from:to:)`'s rules. Nil when
    /// nothing changed.
    init?(from original: Trip, to form: TripForm) {
        self.init(
            date: form.date == original.date ? nil : form.date,
            details: TripDetailsPatch(
                from: (original.miles, original.purpose, original.startLocation, original.endLocation), to: form.details
            )
        )
        if self == TripPatch() { return nil }
    }
}

/// The saved-trip sheet's fields as entered.
struct SavedTripFields: Equatable {
    var name = ""
    var details = TripDetailFields()

    /// A name of 1-`maxNameLength` characters once trimmed, then the details' rules.
    var validated: Result<SavedTripForm, MileageFormRule> {
        guard let name = name.trimmedOrNil, name.count <= MileageFormRule.maxNameLength else { return .failure(.name) }
        return details.validated.map { SavedTripForm(name: name, details: $0) }
    }
}

extension SavedTripFields {
    /// The fields of `saved`, for an edit.
    init(editing saved: SavedTrip) {
        self.init(
            name: saved.name,
            details: TripDetailFields(
                miles: saved.miles, purpose: saved.purpose, startLocation: saved.startLocation,
                endLocation: saved.endLocation
            )
        )
    }
}

/// The saved-trip sheet's fields, validated.
struct SavedTripForm: Equatable {
    var name: String
    var details: TripDetails
}

extension SavedTripDraft {
    /// A draft of what `form` holds, under `id` so a retry sends the same one.
    init(id: UUID, form: SavedTripForm) {
        self.init(
            id: id, name: form.name, miles: form.details.miles, purpose: form.details.purpose,
            startLocation: form.details.startLocation, endLocation: form.details.endLocation
        )
    }
}

extension SavedTripPatch {
    /// The fields where `form` differs from `original`, by `TripDetailsPatch.init(from:to:)`'s rules; the name is
    /// compared trimmed too. Nil when nothing changed.
    init?(from original: SavedTrip, to form: SavedTripForm) {
        self.init(
            name: form.name == original.name.trimmedOrNil ? nil : form.name,
            details: TripDetailsPatch(
                from: (original.miles, original.purpose, original.startLocation, original.endLocation), to: form.details
            )
        )
        if self == SavedTripPatch() { return nil }
    }
}

/// The odometer sheet's readings as entered.
struct OdometerFields: Equatable {
    var startText = ""
    var endText = ""

    /// A start reading with at most one decimal, and an end reading (optional) no lower than it.
    var validated: Result<OdometerInput, MileageFormRule> {
        guard let start = Miles(userInput: startText) else { return .failure(.startReading) }
        guard let end = endText.parsedUnlessBlank(Miles.init(userInput:)) else { return .failure(.endReading) }
        if let end, end < start { return .failure(.endBeforeStart) }
        return .success(OdometerInput(startReading: start, endReading: end))
    }
}

extension OdometerFields {
    /// The fields of `reading`, or blank when the year has none.
    init(editing reading: OdometerReading?) {
        self.init(startText: reading?.startReading.inputText ?? "", endText: reading?.endReading?.inputText ?? "")
    }
}

/// The rate sheet's fields as entered. A new one starts today.
struct MileageRateFields: Equatable {
    var effectiveFrom = CalendarDate(date: .now)
    var rateText = ""

    /// A rate above zero with at most four decimals.
    var validated: Result<MileageRateForm, MileageFormRule> {
        guard let rate = PerMileRate(userInput: rateText), rate.value > 0 else { return .failure(.rate) }
        return .success(MileageRateForm(effectiveFrom: effectiveFrom, rate: rate))
    }
}

extension MileageRateFields {
    /// The fields of `rate`, for an edit.
    init(editing rate: MileageRate) {
        self.init(effectiveFrom: rate.effectiveFrom, rateText: rate.rate.inputText)
    }
}

/// The rate sheet's fields, validated.
struct MileageRateForm: Equatable {
    var effectiveFrom: CalendarDate
    var rate: PerMileRate
}

extension MileageRatePatch {
    /// The fields where `form` differs from `original`; nil when nothing changed.
    init?(from original: MileageRate, to form: MileageRateForm) {
        self.init(
            effectiveFrom: form.effectiveFrom == original.effectiveFrom ? nil : form.effectiveFrom,
            rate: form.rate == original.rate ? nil : form.rate
        )
        if self == MileageRatePatch() { return nil }
    }
}
