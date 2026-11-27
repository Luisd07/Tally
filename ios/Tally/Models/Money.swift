import Foundation

/// A USD amount. The API sends and accepts money as JSON strings (`"156.60"`), never floats.
/// Encoding always writes two decimal places (the server's `decimal_places=2`), so `"220"` is sent as `"220.00"`.
struct Money: Codable, Hashable, Comparable {
    var amount: Decimal

    init(_ amount: Decimal) {
        self.amount = amount
    }

    init(from decoder: Decoder) throws {
        let string = try decoder.singleValueContainer().decode(String.self)
        guard let amount = Decimal(string: string, locale: Self.posix) else {
            throw DecodingError.dataCorrupted(
                .init(codingPath: decoder.codingPath, debugDescription: "Invalid money \(string)")
            )
        }
        self.init(amount)
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(inputText)
    }

    /// An amount the user typed or pasted: digits, optionally grouped with commas in the right places
    /// ("1,000"), then an optional "." and at most two decimals. Surrounding whitespace is ignored.
    /// Anything else (a sign, letters, a second ".", a third decimal) is nil, never a truncated number.
    init?(userInput text: String) {
        guard let trimmed = text.trimmedOrNil,
              trimmed.wholeMatch(of: /(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{0,2})?|\.\d{1,2}/) != nil,
              let amount = Decimal(string: trimmed.replacing(",", with: ""), locale: Self.posix)
        else { return nil }
        self.init(amount)
    }

    /// Two decimals, no grouping, "." as the separator: what the server takes and `init(userInput:)` reads.
    var inputText: String {
        amount.formatted(Decimal.FormatStyle(locale: Self.posix).grouping(.never).precision(.fractionLength(2)))
    }

    /// The same amount with the sign flipped: an outflow shown as the positive cost it is, or back.
    var negated: Money { Money(-amount) }

    /// The amount as a Double, for plotting in a chart only; arithmetic stays in `Decimal`.
    var chartValue: Double { Double(truncating: amount as NSDecimalNumber) }

    /// Rounded to whole cents, half to even, as the server quantizes its sums.
    var roundedToCents: Money {
        var input = amount
        var result = Decimal()
        NSDecimalRound(&result, &input, 2, .bankers)
        return Money(result)
    }

    static func < (lhs: Money, rhs: Money) -> Bool { lhs.amount < rhs.amount }
    static func + (lhs: Money, rhs: Money) -> Money { Money(lhs.amount + rhs.amount) }
    static func - (lhs: Money, rhs: Money) -> Money { Money(lhs.amount - rhs.amount) }

    func formatted() -> String {
        amount.formatted(.currency(code: "USD"))
    }

    private static let posix = Locale(identifier: "en_US_POSIX")
}
