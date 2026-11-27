import Foundation

/// How a CSV's single amount column signs money; unknown server values decode to `.other`.
enum SignConvention: PickableOpenEnum, Hashable {
    case negativeIsOut, positiveIsOut, other(String)

    nonisolated static var allKnown: [SignConvention] { [.negativeIsOut, .positiveIsOut] }
    nonisolated static func unknown(_ raw: String) -> SignConvention { .other(raw) }

    /// The wire string, for requests. The only place the sign convention strings are spelled.
    nonisolated var rawValue: String {
        switch self {
        case .negativeIsOut: "negative_is_out"
        case .positiveIsOut: "positive_is_out"
        case .other(let raw): raw
        }
    }

    var label: String {
        switch self {
        case .negativeIsOut: "Spending is negative"
        case .positiveIsOut: "Spending is positive"
        case .other(let raw): raw.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }
}

/// Where a CSV's amounts are: one signed column, or a debit column and a credit column.
enum AmountColumns: Hashable {
    case single(String)
    case debitCredit(debit: String, credit: String)

    /// The column references, in order.
    var columns: [String] {
        switch self {
        case .single(let column): [column]
        case .debitCredit(let debit, let credit): [debit, credit]
        }
    }
}

/// Mirrors `ImportMappingIn` (sent) and `ImportMappingOut` (read; its ids are ignored). The amount is one column or
/// a debit and credit pair, never both. Without a header, columns are 0-based indexes like "0".
struct ImportMapping: Codable, Hashable {
    var dateColumn: String
    var descriptionColumn: String
    var amount: AmountColumns
    /// A `strptime` format, like "%m/%d/%Y".
    var dateFormat: String
    var signConvention: SignConvention
    var hasHeader: Bool

    /// Every column the mapping reads.
    var columns: [String] { [dateColumn, descriptionColumn] + amount.columns }

    init(
        dateColumn: String,
        descriptionColumn: String,
        amount: AmountColumns,
        dateFormat: String,
        signConvention: SignConvention = .negativeIsOut,
        hasHeader: Bool = true
    ) {
        self.dateColumn = dateColumn
        self.descriptionColumn = descriptionColumn
        self.amount = amount
        self.dateFormat = dateFormat
        self.signConvention = signConvention
        self.hasHeader = hasHeader
    }

    private enum CodingKeys: String, CodingKey {
        case dateColumn, descriptionColumn, amountColumn, debitColumn, creditColumn, dateFormat, signConvention, hasHeader
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        dateColumn = try container.decode(String.self, forKey: .dateColumn)
        descriptionColumn = try container.decode(String.self, forKey: .descriptionColumn)
        dateFormat = try container.decode(String.self, forKey: .dateFormat)
        signConvention = try container.decode(SignConvention.self, forKey: .signConvention)
        hasHeader = try container.decode(Bool.self, forKey: .hasHeader)
        if let column = try container.decodeIfPresent(String.self, forKey: .amountColumn) {
            amount = .single(column)
        } else {
            amount = .debitCredit(
                debit: try container.decode(String.self, forKey: .debitColumn),
                credit: try container.decode(String.self, forKey: .creditColumn)
            )
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(dateColumn, forKey: .dateColumn)
        try container.encode(descriptionColumn, forKey: .descriptionColumn)
        switch amount {
        case .single(let column):
            try container.encode(column, forKey: .amountColumn)
        case .debitCredit(let debit, let credit):
            try container.encode(debit, forKey: .debitColumn)
            try container.encode(credit, forKey: .creditColumn)
        }
        try container.encode(dateFormat, forKey: .dateFormat)
        try container.encode(signConvention, forKey: .signConvention)
        try container.encode(hasHeader, forKey: .hasHeader)
    }
}

/// Mirrors `ImportRowError`: a CSV line or OFX transaction the server couldn't read.
struct ImportRowError: Decodable, Hashable {
    let row: Int
    let message: String
}

/// Mirrors `ImportResult`, what `POST /api/imports/csv` and `/ofx` did with a file.
struct ImportResult: Decodable, Hashable {
    let inserted: Int
    let skippedDuplicate: Int
    let skippedSyncedRange: Int
    let errors: [ImportRowError]

    /// "3 added · 0 duplicates · 0 already synced".
    var summary: String {
        "\(inserted) added · \(skippedDuplicate) duplicates · \(skippedSyncedRange) already synced"
    }
}

/// Body for `POST /api/ai/guess-mapping`.
struct GuessMappingBody: Encodable {
    let records: [[String]]
}
