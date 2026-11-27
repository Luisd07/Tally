import Foundation

/// An error from talking to the Tally API.
enum APIError: Error, LocalizedError {
    struct ServerError {
        let status: Int
        let code: String
        let message: String
        let requestId: String?
    }

    /// A non-2xx response, from the server's error envelope when present.
    case server(ServerError)
    /// The request never produced an HTTP response.
    case transport(URLError)
    /// The request body couldn't be encoded, so nothing was sent.
    case encoding(EncodingError)
    /// A 2xx response whose body didn't match the expected model.
    case decoding(DecodingError)

    var errorDescription: String? {
        switch self {
        case .server(let error): error.message
        case .transport(let error): error.localizedDescription
        case .encoding: "The app couldn't prepare the request."
        case .decoding: "The server sent a response the app couldn't read."
        }
    }

    var isUnauthorized: Bool { status == 401 }

    /// The HTTP status for server errors; nil when no response was received or readable.
    var status: Int? {
        if case .server(let error) = self { error.status } else { nil }
    }

    var code: String? {
        if case .server(let error) = self { error.code } else { nil }
    }

    var requestId: String? {
        if case .server(let error) = self { error.requestId } else { nil }
    }

    /// Any thrown error as an `APIError`: an `APIError` is kept, anything else becomes a transport failure.
    init(wrapping error: any Error) {
        self = error as? APIError ?? .transport(wrapping: error)
    }

    /// A transport failure for an error that isn't a `URLError`, kept as the underlying error.
    static func transport(wrapping error: any Error) -> APIError {
        if let urlError = error as? URLError { return .transport(urlError) }
        return .transport(URLError(.unknown, userInfo: [NSUnderlyingErrorKey: error]))
    }

    /// Builds a server error from a non-2xx response, falling back to the
    /// `X-Request-ID` header and a generic message when the body isn't the envelope.
    static func from(status: Int, body: Data, headerRequestId: String?) -> APIError {
        let envelope = try? JSONDecoder().decode(Envelope.self, from: body).error
        return .server(
            ServerError(
                status: status,
                code: envelope?.code ?? "http_\(status)",
                message: envelope?.displayMessage ?? "Request failed (HTTP \(status)).",
                requestId: envelope?.requestId ?? headerRequestId
            )
        )
    }

    private struct Envelope: Decodable {
        let error: Detail

        struct Detail: Decodable {
            let code: String
            let message: String
            let requestId: String?
            let details: [Reason]?

            struct Reason: Decodable {
                let loc: [Location]?
                let msg: String?

                /// The field `loc` ends at: nil for the whole body or a list index.
                var field: String? {
                    guard let name = loc?.last?.name, name != "body" else { return nil }
                    return name
                }
            }

            /// One step of a `loc` path: a field name, or nil for a list index.
            struct Location: Decodable {
                let name: String?

                init(from decoder: Decoder) throws {
                    name = try? decoder.singleValueContainer().decode(String.self)
                }
            }

            /// The prefix pydantic puts on a validator's `ValueError` message (and the server's
            /// `UnprocessableEntity.value_error`, api/app/errors.py).
            private static let valueErrorPrefix = "Value error, "

            /// For validation errors the server's reason (`details[0].msg`, minus `valueErrorPrefix`)
            /// beats the generic message, after its field when `loc` names one:
            /// "amount: Input should be greater than 0".
            var displayMessage: String {
                guard code == "validation_error", let first = details?.first, var reason = first.msg else {
                    return message
                }
                if reason.hasPrefix(Self.valueErrorPrefix) { reason.removeFirst(Self.valueErrorPrefix.count) }
                reason = reason.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !reason.isEmpty else { return message }
                return first.field.map { "\($0): \(reason)" } ?? reason
            }

            enum CodingKeys: String, CodingKey {
                case code, message, details
                case requestId = "request_id"
            }
        }
    }
}
