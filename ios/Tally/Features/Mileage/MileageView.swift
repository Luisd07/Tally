import SwiftUI

/// The mileage log: a year's business trips with their total, the saved trips that log one with a tap, and the
/// links to the year's odometer and the per-mile rates.
struct MileageView: View {
    @Environment(APIClient.self) private var api
    @State private var model = MileageModel()
    @State private var saved = SavedTripsModel()
    @State private var year = MonthRef().year
    @State private var sheet: MileageSheet?

    var body: some View {
        content
            .navigationTitle("Mileage")
            .navigationBarTitleDisplayMode(.inline)
            .safeAreaInset(edge: .top, spacing: 0) { YearSwitcher(year: $year) }
            .safeAreaInset(edge: .bottom, spacing: 0) { undoBanner }
            .task { await saved.load(api: api) }
            .task(id: year) { await loadTrips() }
            .refreshable { await load() }
            .reloadOnReturnToForeground { await load() }
            .sheet(item: $sheet) { sheet in
                switch sheet {
                case .addTrip: TripFormSheet(model: model, trip: nil)
                case .editTrip(let trip): TripFormSheet(model: model, trip: trip)
                case .addSaved: SavedTripFormSheet(model: saved, saved: nil)
                case .editSaved(let row): SavedTripFormSheet(model: saved, saved: row)
                }
            }
            .editFailureAlert(model.editError ?? saved.editError) {
                model.clearEditError()
                saved.clearEditError()
            }
    }

    private var content: some View {
        MonthContent(
            hasData: model.hasData,
            error: model.error,
            failedTitle: "Couldn't load Mileage",
            retry: { Task { await loadTrips() } }
        ) { list }
    }

    private var list: some View {
        List {
            if let error = model.error {
                RetrySection(title: "Couldn't refresh", error: error) { Task { await loadTrips() } }
            }
            if let failed = model.failedLog {
                failedLogSection(failed)
            }
            if let error = saved.error {
                RetrySection(title: "Couldn't load saved trips", error: error) { Task { await saved.load(api: api) } }
            }
            savedTripsSection
            yearSection
            tripsSection
        }
    }

    private func failedLogSection(_ failed: MileageModel.FailedLog) -> some View {
        Section {
            Label(failed.error.localizedDescription, systemImage: "exclamationmark.triangle")
                .foregroundStyle(.red)
            Button("Retry") { Task { await model.retryLog(api: api) } }
            Button("Dismiss", action: model.dismissFailedLog)
        } header: {
            Text("Couldn't log \(failed.saved.name)")
        }
    }

    private var savedTripsSection: some View {
        Section {
            ForEach(saved.savedTrips) { row in savedTripButton(row) }
            Button("Add saved trip", systemImage: "plus.circle.fill") { sheet = .addSaved }
        } header: {
            Text("Saved trips")
        } footer: {
            Text("Tap a saved trip to log it for today. Swipe to edit or delete it.")
        }
    }

    private func savedTripButton(_ row: SavedTrip) -> some View {
        Button { Task { await model.log(row, api: api) } } label: {
            HStack {
                Label(row.name, systemImage: "plus.circle")
                Spacer()
                Text(row.miles.formatted()).foregroundStyle(.secondary)
            }
            .foregroundStyle(.primary)
        }
        .disabled(model.loggingIds.contains(row.id))
        .swipeActions(edge: .trailing) {
            Button("Delete", role: .destructive) { Task { await saved.delete(row, api: api) } }
            Button("Edit") { sheet = .editSaved(row) }
        }
    }

    private var yearSection: some View {
        Section(String(year)) {
            LabeledContent("Business miles", value: model.businessMiles.formatted())
            LabeledContent("Trips", value: "\(model.trips.count)")
            NavigationLink("Odometer") { OdometerView(year: year) }
            NavigationLink("Rates") { MileageRatesView() }
        }
    }

    private var tripsSection: some View {
        Section("Trips") {
            Button("Add trip", systemImage: "plus.circle.fill") { sheet = .addTrip }
            if model.trips.isEmpty {
                Text("No trips in \(String(year))").foregroundStyle(.secondary)
            }
            ForEach(model.trips) { trip in
                Button { sheet = .editTrip(trip) } label: { TripRow(trip: trip) }
                    .swipeActions(edge: .trailing) {
                        Button("Delete", role: .destructive) { Task { await model.delete(trip, api: api) } }
                    }
            }
        }
    }

    @ViewBuilder
    private var undoBanner: some View {
        if let logged = model.lastLogged {
            UndoBanner(
                message: "Logged \(logged.name), \(logged.trip.miles.formatted())",
                undo: { Task { await model.undoLog(api: api) } },
                expire: { model.expireUndo(id: logged.id) }
            )
            .id(logged.id)
        }
    }

    private func load() async {
        async let savedLoad: Void = saved.load(api: api)
        await loadTrips()
        await savedLoad
    }

    private func loadTrips() async {
        await model.load(year: year, api: api)
    }
}

/// A trip's purpose and miles, then its date and route.
private struct TripRow: View {
    let trip: Trip

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(trip.purpose)
                Spacer()
                Text(trip.miles.formatted()).foregroundStyle(.secondary)
            }
            Text(detail).font(.caption).foregroundStyle(.secondary)
        }
        .foregroundStyle(.primary)
    }

    private var detail: String {
        var parts = [trip.date.formatted()]
        switch (trip.startLocation, trip.endLocation) {
        case let (start?, end?): parts.append("\(start) → \(end)")
        case let (start?, nil): parts.append("From \(start)")
        case let (nil, end?): parts.append("To \(end)")
        case (nil, nil): break
        }
        return parts.joined(separator: " · ")
    }
}

private enum MileageSheet: Identifiable {
    case addTrip
    case editTrip(Trip)
    case addSaved
    case editSaved(SavedTrip)

    var id: String {
        switch self {
        case .addTrip: "addTrip"
        case .editTrip(let trip): trip.id.uuidString
        case .addSaved: "addSaved"
        case .editSaved(let row): row.id.uuidString
        }
    }
}
