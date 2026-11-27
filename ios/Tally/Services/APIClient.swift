import Foundation
import Observation

/// Where requests go and which token they carry, read fresh for every request. `Session` is the app's source.
protocol APICredentialsSource: AnyObject {
    var serverURL: URL { get }
    var accessToken: String? { get }
    /// Called when a request sent with `token` got a 401 that renewing couldn't fix.
    func tokenRejected(_ token: String)
    /// Called first when a request sent with `rejected` got a 401: `.renewed` means retry with the current token.
    func renew(after rejected: String) async -> TokenRenewal
}

extension APICredentialsSource {
    func renew(after rejected: String) async -> TokenRenewal { .rejected }
}

/// True inside the outbox's own sends, so they don't schedule another flush through `APIClient.onSuccess`.
nonisolated enum SuppressOnSuccess {
    @TaskLocal static var isActive = false
}

@Observable
final class APIClient {
    /// Placeholder response type for endpoints that return 204 No Content.
    struct Empty: Decodable {}

    private struct NoBody: Encodable {}

    private static let encoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        return encoder
    }()

    /// The app's session. Ephemeral and without a `URLCache`, so no response, financial or not, reaches disk.
    static let defaultSession: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: configuration)
    }()

    private let credentials: any APICredentialsSource
    private let urlSession: URLSession

    /// Called after every request that succeeded and decoded, except the outbox's own. One slot: a second
    /// subscriber would replace the app's outbox flush, so fan out from the single closure.
    @ObservationIgnored var onSuccess: (() -> Void)?

    init(credentials: any APICredentialsSource, urlSession: URLSession = APIClient.defaultSession) {
        self.credentials = credentials
        self.urlSession = urlSession
    }

    /// Throws `CancellationError` when cancelled and `APIError` for every other failure.
    func get<T: Decodable>(_ path: String, query: [URLQueryItem] = []) async throws -> T {
        try await send("GET", path, query: query, body: nil as NoBody?)
    }

    /// A `DELETE` without a body, for an endpoint that answers 204 No Content. Same error contract as `get`.
    func delete(_ path: String) async throws {
        let _: Empty = try await send("DELETE", path, body: nil as NoBody?)
    }

    /// `timeout` replaces URLSession's 60 s default for slow, Claude-backed endpoints. Throws `CancellationError`
    /// when cancelled and `APIError` for every other failure.
    func send<T: Decodable, B: Encodable>(
        _ method: String,
        _ path: String,
        query: [URLQueryItem] = [],
        body: B?,
        timeout: TimeInterval? = nil
    ) async throws -> T {
        let encoded = try body.map { (data: try encode($0), contentType: "application/json") }
        return try await perform(method, path, query: query, body: encoded, timeout: timeout)
    }

    /// Sends `parts` as `multipart/form-data`. Same error contract as `send`.
    func sendMultipart<T: Decodable>(
        _ method: String,
        _ path: String,
        query: [URLQueryItem] = [],
        parts: [MultipartPart]
    ) async throws -> T {
        let boundary = "tally-\(UUID().uuidString)"
        let body = (data: MultipartPart.encode(parts, boundary: boundary), contentType: "multipart/form-data; boundary=\(boundary)")
        return try await perform(method, path, query: query, body: body)
    }

    /// The raw bytes of a successful `GET`, for responses that aren't JSON (the tax export zip). Same error
    /// contract as `get`.
    func download(_ path: String, query: [URLQueryItem] = []) async throws -> Data {
        let data = try await fetch("GET", path, query: query, body: nil)
        notifySuccess()
        return data
    }

    private func perform<T: Decodable>(
        _ method: String,
        _ path: String,
        query: [URLQueryItem],
        body: (data: Data, contentType: String)?,
        timeout: TimeInterval? = nil
    ) async throws -> T {
        let data = try await fetch(method, path, query: query, body: body, timeout: timeout)
        let decoded: T = try decode(data)
        notifySuccess()
        return decoded
    }

    private func notifySuccess() {
        if !SuppressOnSuccess.isActive { onSuccess?() }
    }

    /// The body of a 2xx response; anything else throws. A 401 renews the token and retries once.
    private func fetch(
        _ method: String,
        _ path: String,
        query: [URLQueryItem],
        body: (data: Data, contentType: String)?,
        timeout: TimeInterval? = nil
    ) async throws -> Data {
        let (request, sentToken) = makeRequest(method, path, query: query, body: body, timeout: timeout)
        let (data, http) = try await transport(request)
        if http.statusCode == 401, let sentToken {
            switch await credentials.renew(after: sentToken) {
            case .renewed:
                let (retry, retryToken) = makeRequest(method, path, query: query, body: body, timeout: timeout)
                let (retryData, retryHTTP) = try await transport(retry)
                try validate(retryData, retryHTTP, sentToken: retryToken)
                return retryData
            case .failed(let error):
                throw error
            case .rejected:
                break
            }
        }
        try validate(data, http, sentToken: sentToken)
        return data
    }

    private func makeRequest(
        _ method: String,
        _ path: String,
        query: [URLQueryItem],
        body: (data: Data, contentType: String)?,
        timeout: TimeInterval? = nil
    ) -> (URLRequest, token: String?) {
        let token = credentials.accessToken
        let url = credentials.serverURL.appending(path: path)
        var request = URLRequest(url: query.isEmpty ? url : url.appending(queryItems: query))
        request.httpMethod = method
        if let timeout { request.timeoutInterval = timeout }
        if let token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            request.httpBody = body.data
            request.setValue(body.contentType, forHTTPHeaderField: "Content-Type")
        }
        return (request, token)
    }

    private func encode<B: Encodable>(_ body: B) throws(APIError) -> Data {
        do {
            return try Self.encoder.encode(body)
        } catch let error as EncodingError {
            throw .encoding(error)
        } catch {
            let context = EncodingError.Context(codingPath: [], debugDescription: "Body failed to encode.", underlyingError: error)
            throw .encoding(.invalidValue(body, context))
        }
    }

    private func transport(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await urlSession.data(for: request)
        } catch let error as URLError where error.code == .cancelled {
            throw CancellationError()
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            throw APIError.transport(wrapping: error)
        }
        guard let http = response as? HTTPURLResponse else {
            throw APIError.transport(URLError(.badServerResponse))
        }
        return (data, http)
    }

    /// Throws for non-2xx responses, telling the credentials source when the token it sent was rejected.
    private func validate(_ data: Data, _ http: HTTPURLResponse, sentToken: String?) throws(APIError) {
        guard !(200..<300).contains(http.statusCode) else { return }
        if http.statusCode == 401, let sentToken {
            credentials.tokenRejected(sentToken)
        }
        throw APIError.from(
            status: http.statusCode,
            body: data,
            headerRequestId: http.value(forHTTPHeaderField: "X-Request-ID")
        )
    }

    private func decode<T: Decodable>(_ data: Data) throws(APIError) -> T {
        // A 204 has no body; callers asking for `Empty` get one without a decode error.
        if data.isEmpty, let empty = Empty() as? T { return empty }
        do {
            return try JSONDecoder.api.decode(T.self, from: data)
        } catch let error as DecodingError {
            throw .decoding(error)
        } catch {
            let context = DecodingError.Context(codingPath: [], debugDescription: "Body failed to decode.", underlyingError: error)
            throw .decoding(.dataCorrupted(context))
        }
    }
}

/// One part of a `multipart/form-data` body.
struct MultipartPart {
    let name: String
    let filename: String?
    let contentType: String?
    let data: Data

    static func field(_ name: String, _ value: String) -> MultipartPart {
        MultipartPart(name: name, filename: nil, contentType: nil, data: Data(value.utf8))
    }

    static func file(_ name: String, filename: String, contentType: String, data: Data) -> MultipartPart {
        MultipartPart(name: name, filename: filename, contentType: contentType, data: data)
    }

    static func encode(_ parts: [MultipartPart], boundary: String) -> Data {
        var body = Data()
        for part in parts {
            var disposition = "Content-Disposition: form-data; name=\"\(part.name)\""
            if let filename = part.filename { disposition += "; filename=\"\(filename)\"" }
            body.append(Data("--\(boundary)\r\n\(disposition)\r\n".utf8))
            if let contentType = part.contentType { body.append(Data("Content-Type: \(contentType)\r\n".utf8)) }
            body.append(Data("\r\n".utf8))
            body.append(part.data)
            body.append(Data("\r\n".utf8))
        }
        body.append(Data("--\(boundary)--\r\n".utf8))
        return body
    }
}
