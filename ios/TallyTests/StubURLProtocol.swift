import Foundation
import os

/// Answers every request from a test-supplied handler instead of the network.
/// The handler runs off the main actor on URLSession's loading thread, so it's stored behind a lock.
/// One handler is shared process-wide, so every test that uses it lives in one `.serialized` suite.
final class StubURLProtocol: URLProtocol, @unchecked Sendable {
    typealias Handler = @Sendable (URLRequest) async throws -> (HTTPURLResponse, Data)

    private static let handler = OSAllocatedUnfairLock<Handler?>(initialState: nil)

    private var loading: Task<Void, Never>?

    static func respond(with handler: @escaping Handler) {
        self.handler.withLock { $0 = handler }
    }

    /// A session whose requests all go to the stub.
    static func makeSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubURLProtocol.self]
        return URLSession(configuration: configuration)
    }

    static func response(to request: URLRequest, status: Int, headers: [String: String] = [:]) -> HTTPURLResponse {
        HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: headers)!
    }

    override class func canInit(with request: URLRequest) -> Bool { true }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let handler = Self.handler.withLock { $0 }
        let request = request
        loading = Task {
            guard let handler else {
                client?.urlProtocol(self, didFailWithError: URLError(.unsupportedURL))
                return
            }
            do {
                let (response, data) = try await handler(request)
                client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
                client?.urlProtocol(self, didLoad: data)
                client?.urlProtocolDidFinishLoading(self)
            } catch {
                client?.urlProtocol(self, didFailWithError: error)
            }
        }
    }

    override func stopLoading() {
        loading?.cancel()
    }
}
