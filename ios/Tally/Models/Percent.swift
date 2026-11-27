import Foundation

/// A percentage from 0 to 100. An APR goes to the server as a two-decimal string (`inputText`); a business
/// share is a whole number (`wholeNumber`), read with `wholeOnly`, because the server takes an integer for it.
struct Percent: Equatable {
    /// Compared as a number, so 24.9 and 24.90 are equal.
    let value: Decimal

    /// What the user typed: digits with an optional "." and at most two decimals, or with `wholeOnly` at most
    /// three digits and no decimals, from 0 to 100. Surrounding whitespace is ignored. Anything else is nil.
    init?(userInput text: String, wholeOnly: Bool = false) {
        guard let trimmed = text.trimmedOrNil,
              trimmed.wholeMatch(of: wholeOnly ? /\d{1,3}/ : /\d+(?:\.\d{0,2})?|\.\d{1,2}/) != nil,
              let value = Decimal(string: trimmed, locale: Self.posix), value <= 100
        else { return nil }
        self.value = value
    }

    /// The server's decimal string, like `"24.99"`.
    init?(apiString: String) {
        guard let value = Decimal(string: apiString, locale: Self.posix) else { return nil }
        self.value = value
    }

    /// Two decimals, no grouping, "." as the separator: what the server takes for an APR and `init(userInput:)` reads.
    var inputText: String {
        value.formatted(Decimal.FormatStyle(locale: Self.posix).grouping(.never).precision(.fractionLength(2)))
    }

    /// The value as an integer, or nil when it has decimals.
    var wholeNumber: Int? {
        let number = NSDecimalNumber(decimal: value)
        return number == NSDecimalNumber(value: number.intValue) ? number.intValue : nil
    }

    private static let posix = Locale(identifier: "en_US_POSIX")
}
