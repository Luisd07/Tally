import Foundation

extension JSONDecoder {
    /// Decoder for API responses: snake_case keys and ISO 8601 timestamps with or without fractional seconds.
    static var api: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        decoder.dateDecodingStrategy = .custom { decoder in
            let string = try decoder.singleValueContainer().decode(String.self)
            let fractional = Date.ISO8601FormatStyle(includingFractionalSeconds: true)
            if let date = try? fractional.parse(string) { return date }
            if let date = try? Date.ISO8601FormatStyle().parse(string) { return date }
            throw DecodingError.dataCorrupted(
                .init(codingPath: decoder.codingPath, debugDescription: "Invalid timestamp \(string)")
            )
        }
        return decoder
    }
}

extension KeyedEncodingContainer {
    /// A PATCH field: skips an unset value (`nil`), writes `null` for a cleared one (`.some(nil)`).
    nonisolated mutating func encodePatchField<T: Encodable>(_ value: T??, forKey key: Key) throws {
        switch value {
        case .none: break
        case .some(.none): try encodeNil(forKey: key)
        case .some(.some(let wrapped)): try encode(wrapped, forKey: key)
        }
    }
}

extension String {
    /// The text without surrounding whitespace, or nil when nothing is left.
    var trimmedOrNil: String? {
        let trimmed = trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    /// An optional field's value: `.some(nil)` when blank, nil when `parse` rejects the trimmed text.
    func parsedUnlessBlank<Value>(_ parse: (String) -> Value?) -> Value?? {
        guard let trimmed = trimmedOrNil else { return .some(nil) }
        return parse(trimmed).map { .some($0) }
    }
}
