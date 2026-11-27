import Foundation
import Testing
@testable import Tally

extension APIClientTests {
    private func csvFilter() -> TransactionFilter {
        TransactionFilter(
            month: MonthRef(year: 2026, month: 10),
            search: " coffee ",
            bucket: .personal,
            accountId: UUID(uuidString: "11111111-2222-3333-4444-555555555555"),
            categoryId: UUID(uuidString: "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE")
        )
    }

    /// Answers the list with an empty page and the CSV request with the fixture, streaming every request seen.
    private func replyToListAndCSV(csv: Data) -> AsyncStream<URLRequest> {
        let (requests, seen) = AsyncStream.makeStream(of: URLRequest.self)
        StubURLProtocol.respond { request in
            seen.yield(request)
            let isCSV = request.url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: false) }?
                .queryItems?.contains(URLQueryItem(name: "format", value: "csv")) == true
            let body = isCSV ? csv : Data(#"{"items":[],"next_cursor":null,"total":0}"#.utf8)
            return (StubURLProtocol.response(to: request, status: 200), body)
        }
        return requests
    }

    private func queryItems(_ request: URLRequest?) throws -> [URLQueryItem] {
        let url = try #require(request?.url)
        #expect(url.path == "/api/transactions")
        return try #require(URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems)
    }

    /// The list sends `limit` (and `cursor` when paging); the CSV is the whole result, so it sends neither.
    @Test func transactionsCSVQueryIsTheRequestTheListSendsPlusFormat() async throws {
        defer { try? fixture.cleanUp(); ExportFile.sweep() }
        let api = makeClient(fixture.make())
        var requests = replyToListAndCSV(csv: try bundleFixture("tally-transactions-2026-10", ext: "csv"))
            .makeAsyncIterator()
        let filter = csvFilter()
        let model = TransactionsModel()
        model.filter = filter

        await model.load(api: api)
        let listItems = try queryItems(await requests.next())
        _ = try await api.transactionsCSV(filter.query())
        let csvItems = try queryItems(await requests.next())

        let paging: Set = ["limit", "cursor"]
        #expect(csvItems == listItems.filter { !paging.contains($0.name) } + [URLQueryItem(name: "format", value: "csv")])
        #expect(csvItems.map(\.name) == ["month", "q", "bucket", "account_id", "category_id", "format"])
    }

    @Test func transactionsCSVIsWrittenAsTheProtectedFixture() async throws {
        defer { try? fixture.cleanUp(); ExportFile.sweep() }
        let api = makeClient(fixture.make())
        let csv = try bundleFixture("tally-transactions-2026-10", ext: "csv")
        _ = reply(200, body: csv)
        let filter = csvFilter()

        let url = try ExportFile.write(
            try await api.transactionsCSV(filter.query()),
            named: ExportFile.transactionsCSVName(month: filter.month),
            in: ExportFile.directory(for: .transactionsCSV)
        )
        #expect(url.lastPathComponent == "tally-transactions-2026-10.csv")
        #expect(try Data(contentsOf: url) == csv)
    }

    @Test func transactionsCSVModelFinishDeletesTheFile() async throws {
        defer { try? fixture.cleanUp(); ExportFile.sweep() }
        let api = makeClient(fixture.make())
        _ = reply(200, body: try bundleFixture("tally-transactions-2026-10", ext: "csv"))
        let filter = csvFilter()
        let model = ExportModel(kind: .transactionsCSV)

        await model.export(named: ExportFile.transactionsCSVName(month: filter.month)) {
            try await api.transactionsCSV(filter.query())
        }
        let url = try #require(model.readyURL)
        #expect(FileManager.default.fileExists(atPath: url.path))

        model.finish()
        #expect(model.state == .idle)
        #expect(!FileManager.default.fileExists(atPath: url.path))
    }

    @Test func transactionsCSVModelShowsTheFailureMessage() async throws {
        defer { try? fixture.cleanUp(); ExportFile.sweep() }
        let api = makeClient(fixture.make())
        _ = reply(500, body: Data(#"{"error":{"code":"server_error","message":"Export failed.","request_id":"r1"}}"#.utf8))
        let filter = csvFilter()
        let model = ExportModel(kind: .transactionsCSV)

        await model.export(named: ExportFile.transactionsCSVName(month: filter.month)) {
            try await api.transactionsCSV(filter.query())
        }
        #expect(model.failureMessage == "Export failed.")
        #expect(model.readyURL == nil)
    }
}
