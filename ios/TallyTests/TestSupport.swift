import Foundation
import os
import Testing
@testable import Tally

/// Helpers the model tests in `APIClientTests` share.
extension APIClientTests {
    /// The exact amount `text` spells, like `"-15.49"`.
    func money(_ text: String) -> Money {
        Money(Decimal(string: text)!)
    }

    /// `body` encoded with snake_case keys, as `APIClient` sends it, read back as a dictionary.
    func snakeCaseKeys(_ body: some Encodable) throws -> [String: Any] {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        return try #require(JSONSerialization.jsonObject(with: encoder.encode(body)) as? [String: Any])
    }

    /// Holds the first GET until `release` yields, then answers it with `page`; later GETs (a reload) are answered
    /// at once with `reloaded`, and other requests with `other`.
    func holdLoads(
        page: Data, reloaded: Data, other: @escaping @Sendable (URLRequest) -> (status: Int, body: String)
    ) -> (arrived: AsyncStream<Void>, release: AsyncStream<Void>.Continuation) {
        let (arrived, arrivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let gets = OSAllocatedUnfairLock(initialState: 0)
        StubURLProtocol.respond { request in
            guard request.httpMethod == "GET" else {
                let answer = other(request)
                return (StubURLProtocol.response(to: request, status: answer.status), Data(answer.body.utf8))
            }
            guard gets.withLock({ $0 += 1; return $0 }) == 1 else {
                return (StubURLProtocol.response(to: request, status: 200), reloaded)
            }
            arrivedSignal.yield()
            for await _ in release { break }
            return (StubURLProtocol.response(to: request, status: 200), page)
        }
        return (arrived, releaseSignal)
    }
}
