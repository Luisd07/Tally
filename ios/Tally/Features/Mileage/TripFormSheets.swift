import SwiftUI

/// Adds a trip, or edits `trip`. An add keeps its id across a failed Save, so a retry can't log a second trip;
/// an edit sends only the fields that changed.
struct TripFormSheet: View {
    let model: MileageModel
    let trip: Trip?

    @Environment(APIClient.self) private var api
    @State private var fields: TripFields

    init(model: MileageModel, trip: Trip?) {
        self.model = model
        self.trip = trip
        _fields = State(initialValue: trip.map(TripFields.init(editing:)) ?? TripFields())
    }

    private var form: Result<TripForm, MileageFormRule> { fields.validated }

    /// A valid form; for an edit, one that changes something.
    private var canSave: Bool {
        guard case .success(let form) = form else { return false }
        guard let trip else { return true }
        return TripPatch(from: trip, to: form) != nil
    }

    var body: some View {
        FormSheet(
            title: trip == nil ? "Add trip" : "Edit trip",
            canSave: canSave,
            invalidReason: form.invalidMessage,
            save: save
        ) {
            DatePicker("Date", selection: $fields.date.asDate, displayedComponents: .date)
            TripDetailInputs(fields: $fields.details)
        }
    }

    /// An edit closes at once and lets the model apply it optimistically; a failure shows as the screen's alert.
    /// An add stays open until the server answers.
    private var save: FormSheetSave {
        guard let trip else { return .waitsForServer(create, failure: { model.createError }) }
        return .closesAtOnce {
            guard case .success(let form) = form, let patch = TripPatch(from: trip, to: form) else { return }
            Task { await model.update(trip, patch: patch, api: api) }
        }
    }

    private func create(id: UUID) async -> Bool {
        guard case .success(let form) = form else { return false }
        return await model.create(TripDraft(id: id, form: form), api: api)
    }
}

/// Adds a saved trip, or edits `saved`, by `TripFormSheet`'s rules.
struct SavedTripFormSheet: View {
    let model: SavedTripsModel
    let saved: SavedTrip?

    @Environment(APIClient.self) private var api
    @State private var fields: SavedTripFields

    init(model: SavedTripsModel, saved: SavedTrip?) {
        self.model = model
        self.saved = saved
        _fields = State(initialValue: saved.map(SavedTripFields.init(editing:)) ?? SavedTripFields())
    }

    private var form: Result<SavedTripForm, MileageFormRule> { fields.validated }

    private var canSave: Bool {
        guard case .success(let form) = form else { return false }
        guard let saved else { return true }
        return SavedTripPatch(from: saved, to: form) != nil
    }

    var body: some View {
        FormSheet(
            title: saved == nil ? "Add saved trip" : "Edit saved trip",
            canSave: canSave,
            invalidReason: form.invalidMessage,
            save: save
        ) {
            TextField("Name", text: $fields.name)
            TripDetailInputs(fields: $fields.details)
        }
    }

    private var save: FormSheetSave {
        guard let saved else { return .waitsForServer(create, failure: { model.createError }) }
        return .closesAtOnce {
            guard case .success(let form) = form, let patch = SavedTripPatch(from: saved, to: form) else { return }
            Task { await model.update(saved, patch: patch, api: api) }
        }
    }

    private func create(id: UUID) async -> Bool {
        guard case .success(let form) = form else { return false }
        return await model.create(SavedTripDraft(id: id, form: form), api: api)
    }
}

/// The miles, purpose and optional route both trip sheets share.
private struct TripDetailInputs: View {
    @Binding var fields: TripDetailFields

    var body: some View {
        TextField("Miles", text: $fields.milesText)
            .keyboardType(.decimalPad)
        TextField("Business purpose", text: $fields.purpose)
        TextField("Start (optional)", text: $fields.startLocation)
        TextField("End (optional)", text: $fields.endLocation)
    }
}
