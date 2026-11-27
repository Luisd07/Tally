import Foundation

/// Mirrors `TaxSummaryOut` from `GET /api/tax/summary`.
struct TaxSummary: Decodable, Equatable {
    let year: Int
    let grossReceipts: Money
    let expensesByLine: [TaxLineSpend]
    let uncategorizedBusiness: UncategorizedBusiness
    let totalDeductible: Money
    let reimbursementsIncluded: Bool
    /// How line 9 (`car_truck`) was built from the mileage log.
    let mileage: MileageSummary
    let notes: [String]

    /// Whether the year has nothing to show: no tax lines, no gross receipts, no spend off a tax line and no
    /// logged miles or actual car costs.
    var isEmpty: Bool {
        expensesByLine.isEmpty && grossReceipts.amount == 0 && uncategorizedBusiness.count == 0 && !mileage.hasActivity
    }
}

/// One tax line's spend and what is deductible of it.
struct TaxLineSpend: Decodable, Equatable, Identifiable {
    /// The server's name for the line, like `office_expense`; not a line number.
    let taxLine: String
    let spent: Money
    /// The blended deductible share, a whole number from 0 to 100 as the server sends it. Nil when the line's
    /// categories net to opposite signs, so no single share describes it.
    let deductiblePct: Int?
    let deductible: Money

    var id: String { taxLine }

    /// The server's name for line 9, built from the mileage log under the standard mileage rate.
    static let carTruck = "car_truck"

    /// `taxLine` as words: "office_expense" is "Office expense". Line 9 says it uses the standard mileage rate.
    var displayName: String {
        if taxLine == Self.carTruck { return "Car and truck (standard mileage)" }
        let words = taxLine.replacing("_", with: " ")
        return words.prefix(1).uppercased() + words.dropFirst()
    }
}

/// Business spending that is on no tax line: no category, or a category without a tax line.
struct UncategorizedBusiness: Decodable, Equatable {
    let spent: Money
    let count: Int
}
