import Foundation

/// A string enum from the server that keeps unknown values as a case of its own instead of failing the response.
protocol OpenEnum: Decodable {
    /// The wire strings this app knows.
    nonisolated static var known: [String: Self] { get }
    nonisolated static func unknown(_ raw: String) -> Self
}

extension OpenEnum {
    nonisolated init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = Self.known[raw] ?? Self.unknown(raw)
    }
}

/// An `OpenEnum` the app also sends; it encodes as its wire string, unknown values included.
protocol EncodableOpenEnum: OpenEnum, Encodable {
    /// The wire string, for requests.
    nonisolated var rawValue: String { get }
}

extension EncodableOpenEnum {
    nonisolated func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }
}

/// An `EncodableOpenEnum` the app offers in a picker. `allKnown` lists the known cases in picker order, and
/// `known` follows from it.
protocol PickableOpenEnum: EncodableOpenEnum {
    /// Every case the app offers, in picker order.
    nonisolated static var allKnown: [Self] { get }
}

extension PickableOpenEnum {
    nonisolated static var known: [String: Self] {
        Dictionary(uniqueKeysWithValues: allKnown.map { ($0.rawValue, $0) })
    }
}

extension PickableOpenEnum where Self: Equatable {
    /// `allKnown`, plus `current` when the app doesn't know it, so it stays selectable and saving without
    /// touching the picker sends it back.
    static func pickerOptions(including current: Self) -> [Self] {
        allKnown.contains(current) ? allKnown : allKnown + [current]
    }
}
