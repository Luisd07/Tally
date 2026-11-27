import Foundation
import Testing
@testable import Tally

/// Lives in `APIClientTests`' serialized suite because both share `StubURLProtocol`'s one handler.
extension APIClientTests {
    private func body(month: String, total: String) -> Data {
        Data(#"{"month":"\#(month)","spending_by_bucket":{"buckets":[],"total":"\#(total)"},"owed_to_you":"0.00","income":{"expected":"0.00","received":"0.00"},"bills_due":[],"last_updated":null}"#.utf8)
    }

    private nonisolated static func month(of request: URLRequest) -> String? {
        URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "month" }?.value
    }

    @Test func slowResponseForEarlierMonthDoesNotReplaceSelectedMonth() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let september = MonthRef(year: 2026, month: 9)
        let october = MonthRef(year: 2026, month: 10)
        let (received, receivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let septemberBody = body(month: "2026-09", total: "1.00")
        let octoberBody = body(month: "2026-10", total: "2.00")
        StubURLProtocol.respond { request in
            if Self.month(of: request) == "2026-09" {
                receivedSignal.yield()
                for await _ in release { break }
                return (StubURLProtocol.response(to: request, status: 200), septemberBody)
            }
            return (StubURLProtocol.response(to: request, status: 200), octoberBody)
        }

        let model = MonthValue<Home>()
        let slow = Task { await model.load(month: september) { try await api.home(month: september) } }
        for await _ in received { break }
        await model.load(month: october) { try await api.home(month: october) }
        #expect(model.value?.month == "2026-10")

        releaseSignal.yield()
        await slow.value
        #expect(model.value?.month == "2026-10")
        #expect(model.error == nil)
    }

    @Test func failedReloadKeepsDataAndSurfacesError() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let october = MonthRef(year: 2026, month: 10)
        let ok = body(month: "2026-10", total: "2.00")
        StubURLProtocol.respond { request in (StubURLProtocol.response(to: request, status: 200), ok) }
        let model = MonthValue<Home>()
        await model.load(month: october) { try await api.home(month: october) }

        StubURLProtocol.respond { request in (StubURLProtocol.response(to: request, status: 503), Data()) }
        await model.load(month: october) { try await api.home(month: october) }
        #expect(model.value?.month == "2026-10")
        #expect(model.error?.status == 503)

        StubURLProtocol.respond { request in (StubURLProtocol.response(to: request, status: 200), ok) }
        await model.load(month: october) { try await api.home(month: october) }
        #expect(model.error == nil)
    }

    @Test func rejectedDevTokenReturnsMessageAndLeavesSessionUntouched() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        let envelope = #"{"error":{"code":"unauthorized","message":"Sign-in required.","request_id":"r1"}}"#
        StubURLProtocol.respond { request in (StubURLProtocol.response(to: request, status: 401), Data(envelope.utf8)) }

        let message = await DevSignInValidator.validate(
            token: "bad",
            serverURL: URL(string: "http://10.0.0.5:8000")!,
            urlSession: StubURLProtocol.makeSession()
        )

        #expect(message == "Sign-in required.")
        #expect(!session.isSignedIn)
        #expect(fixture.defaults.string(forKey: "server_url") == nil)
        #expect(try Keychain.read(fixture.tokenKey) == nil)
    }

    @Test func acceptedDevTokenIsSentToCandidateServer() async throws {
        let (requests, seen) = AsyncStream.makeStream(of: URLRequest.self)
        let ok = body(month: "2026-10", total: "0.00")
        StubURLProtocol.respond { request in
            seen.yield(request)
            return (StubURLProtocol.response(to: request, status: 200), ok)
        }
        var iterator = requests.makeAsyncIterator()

        let message = await DevSignInValidator.validate(
            token: "good",
            serverURL: URL(string: "http://10.0.0.5:8000")!,
            urlSession: StubURLProtocol.makeSession()
        )

        #expect(message == nil)
        let request = try #require(await iterator.next())
        #expect(request.url?.host() == "10.0.0.5")
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer good")
    }
}
