import Foundation

/// The part of `EventOut` from `GET /api/events` that the event picker needs.
struct Event: Decodable, Identifiable, Hashable {
    let id: UUID
    let name: String
    let startDate: CalendarDate
    let endDate: CalendarDate
}
