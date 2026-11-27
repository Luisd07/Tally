import Foundation

/// A validated manual entry (the Add screen, or the Shortcuts action later) and the rules that turn it into a `Capture`.
struct ManualCapture {
    enum Direction: Hashable {
        case expense, moneyIn
    }

    /// Always positive; `direction` decides the sign of the stored amount.
    var amount: Money
    var direction: Direction
    var accountId: UUID
    var bucket: Bucket
    var categoryId: UUID?
    /// Used as the description when none is typed.
    var categoryName: String?
    var eventId: UUID?
    var date: CalendarDate
    var description = ""
    var merchant = ""
    var notes = ""

    /// Expenses are stored negative and money in positive. The description is the trimmed text, else the
    /// category name, else "Manual expense" or "Manual income"; merchant and notes are trimmed, or nil when empty.
    func capture(id: UUID = UUID()) -> Capture {
        Capture(
            id: id,
            accountId: accountId,
            postedDate: date,
            amount: direction == .expense ? Money(-amount.amount) : amount,
            descriptionRaw: description.trimmedOrNil ?? categoryName ?? fallbackDescription,
            merchant: merchant.trimmedOrNil,
            categoryId: categoryId,
            bucket: bucket,
            notes: notes.trimmedOrNil,
            eventId: eventId
        )
    }

    private var fallbackDescription: String {
        direction == .expense ? "Manual expense" : "Manual income"
    }
}
