import Foundation
import os
import Testing
import UIKit
@testable import Tally

extension APIClientTests {
    private func missingRow(id: UUID) -> String {
        """
        {"id":"\(id.uuidString.lowercased())","account_id":"265cd02e-4a77-51ea-ad3a-4a9fbc282065","posted_date":"2026-10-08","transacted_at":null,"amount":"-85.00","description_raw":"HERTZ","merchant":null,"category_id":null,"bucket":"business","source":"manual","external_id":null,"pending":false,"reviewed":false,"notes":null,"event_id":null,"parent_id":null,"reimbursement_id":null,"is_split":false,"created_at":"2026-10-18T20:42:06.089964Z","updated_at":"2026-10-18T20:42:06.089964Z"}
        """
    }

    private func missingPage(_ ids: [UUID], cursor: String? = nil, count: Int) -> Data {
        let cursorJSON = cursor.map { "\"\($0)\"" } ?? "null"
        return Data(#"{"items":[\#(ids.map(missingRow).joined(separator: ","))],"next_cursor":\#(cursorJSON),"count":\#(count)}"#.utf8)
    }

    private func missingPNG() -> Data {
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        return UIGraphicsImageRenderer(size: CGSize(width: 40, height: 30), format: format).pngData { context in
            UIColor.red.setFill()
            context.fill(CGRect(x: 0, y: 0, width: 40, height: 30))
        }
    }

    private func loadedFixtureModel(_ api: APIClient) async throws -> (MissingReceiptsModel, [UUID]) {
        _ = reply(200, body: try jsonFixture("missing-reimbursable-2026"))
        let model = MissingReceiptsModel()
        model.bucket = .reimbursable
        await model.load(year: 2026, api: api)
        return (model, model.items.map(\.id))
    }

    private func receiptCreated(for transactionId: UUID) -> Data {
        Data(#"{"id":"\#(UUID().uuidString.lowercased())","transaction_id":"\#(transactionId.uuidString.lowercased())","mime":"image/jpeg","sha256":"abc123","uploaded_at":"2026-10-18T20:42:06.089964Z"}"#.utf8)
    }

    @Test func missingReceiptFixtureDecodes() throws {
        let page = try JSONDecoder.api.decode(MissingReceiptPage.self, from: jsonFixture("missing-reimbursable-2026"))
        #expect(page.count == 3)
        #expect(page.items.count == 3)
        #expect(page.nextCursor == nil)
        #expect(page.items.first?.displayName == "HERTZ RENTAL CAR")
    }

    @Test func missingReceiptsSendsYearBucketAndCursor() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let requests = reply(200, body: missingPage([], count: 0))
        var iterator = requests.makeAsyncIterator()

        _ = try await api.missingReceipts(year: 2026, bucket: .business)
        _ = try await api.missingReceipts(year: 2025, bucket: .reimbursable, limit: 200, cursor: "abc")
        let first = try #require(await iterator.next())
        let second = try #require(await iterator.next())

        #expect(first.url?.path() == "/api/tax/missing-receipts")
        #expect(queryItems(first) == [URLQueryItem(name: "year", value: "2026"), URLQueryItem(name: "bucket", value: "business")])
        #expect(queryItems(second) == [
            URLQueryItem(name: "year", value: "2025"),
            URLQueryItem(name: "bucket", value: "reimbursable"),
            URLQueryItem(name: "limit", value: "200"),
            URLQueryItem(name: "cursor", value: "abc"),
        ])
    }

    @Test func missingReceiptsModelLoadsTwoPages() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = (0..<3).map { _ in UUID() }
        let firstPage = missingPage(Array(ids[0..<2]), cursor: "next", count: 3)
        let lastPage = missingPage([ids[2]], count: 3)
        StubURLProtocol.respond { request in
            let hasCursor = request.url?.query()?.contains("cursor=next") == true
            return (StubURLProtocol.response(to: request, status: 200), hasCursor ? lastPage : firstPage)
        }
        let model = MissingReceiptsModel()

        await model.load(year: 2026, api: api)
        #expect(model.items.map(\.id) == Array(ids[0..<2]))
        #expect(model.hasMore)
        #expect(model.count == 3)
        await model.loadMore(api: api)

        #expect(model.items.map(\.id) == ids)
        #expect(!model.hasMore)
        #expect(model.count == 3)
        #expect(model.hasData)
    }

    @Test func missingReceiptsModelIgnoresAStaleFilterResponse() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let business = UUID()
        let reimbursable = UUID()
        let (arrived, arrivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let businessPage = missingPage([business], count: 1)
        let reimbursablePage = missingPage([reimbursable], count: 1)
        StubURLProtocol.respond { request in
            if request.url?.query()?.contains("bucket=business") == true {
                arrivedSignal.yield()
                for await _ in release { break }
                return (StubURLProtocol.response(to: request, status: 200), businessPage)
            }
            return (StubURLProtocol.response(to: request, status: 200), reimbursablePage)
        }
        let model = MissingReceiptsModel()
        let stale = Task { await model.load(year: 2026, api: api) }
        for await _ in arrived { break }

        model.bucket = .reimbursable
        await model.load(year: 2026, api: api)
        releaseSignal.yield()
        await stale.value

        #expect(model.items.map(\.id) == [reimbursable])
        #expect(model.error == nil)
    }

    @Test func failedMissingReceiptsRefreshKeepsRowsAndSetsError() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let (model, ids) = try await loadedFixtureModel(api)
        _ = reply(500, body: Data(#"{"error":{"code":"internal","message":"Boom.","request_id":"r3"}}"#.utf8))

        await model.load(year: 2026, api: api)

        #expect(model.items.map(\.id) == ids)
        #expect(model.count == 3)
        #expect(model.hasData)
        #expect(model.error?.errorDescription == "Boom.")
    }

    private func attachedFixtureModel(_ api: APIClient) async throws -> (MissingReceiptsModel, [UUID], ReceiptUploads) {
        let (model, ids) = try await loadedFixtureModel(api)
        let target = ids[1]
        let png = missingPNG()
        _ = reply(201, body: receiptCreated(for: target))
        let uploads = ReceiptUploads(doneDisplay: .seconds(60), onUploaded: { model.receiptAttached($0) })
        await uploads.attach(to: target, name: "Autozone", api: api) { png }
        return (model, ids, uploads)
    }

    @Test func successfulReceiptUploadRemovesTheRow() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())

        let (model, ids, uploads) = try await attachedFixtureModel(api)

        #expect(uploads.uploads.first?.state == .done)
        #expect(model.items.map(\.id) == [ids[0], ids[2]])
    }

    @Test func successfulReceiptUploadCountsTheRowOff() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())

        let (model, _, _) = try await attachedFixtureModel(api)

        #expect(model.count == 2)
    }

    @Test func aRepeatedReceiptAttachedCountsTheRowOffOnce() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let (model, ids) = try await loadedFixtureModel(api)

        model.receiptAttached(ids[1])
        model.receiptAttached(ids[1])

        #expect(model.items.map(\.id) == [ids[0], ids[2]])
        #expect(model.count == 2)
    }

    @Test func aRefreshInFlightDuringAnUploadDoesNotBringTheRowBack() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let (model, ids) = try await loadedFixtureModel(api)
        let target = ids[1]
        let (arrived, arrivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let stalePage = missingPage(ids, count: 3)
        let created = receiptCreated(for: target)
        StubURLProtocol.respond { request in
            if request.httpMethod == "POST" {
                return (StubURLProtocol.response(to: request, status: 201), created)
            }
            arrivedSignal.yield()
            for await _ in release { break }
            return (StubURLProtocol.response(to: request, status: 200), stalePage)
        }
        let uploads = ReceiptUploads(doneDisplay: .seconds(60), onUploaded: { model.receiptAttached($0) })
        let png = missingPNG()
        let refresh = Task { await model.load(year: 2026, api: api) }
        for await _ in arrived { break }

        await uploads.attach(to: target, name: "Autozone", api: api) { png }
        #expect(uploads.uploads.first?.state == .done)
        releaseSignal.yield()
        await refresh.value

        #expect(model.items.map(\.id) == [ids[0], ids[2]])
        #expect(model.count == 2)
    }

    @Test func aNextPageInFlightWhenAReceiptLandsDropsTheRow() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = (0..<3).map { _ in UUID() }
        let (arrived, arrivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let firstPage = missingPage(Array(ids[0..<2]), cursor: "next", count: 3)
        let lastPage = missingPage([ids[2]], count: 3)
        StubURLProtocol.respond { request in
            guard request.url?.query()?.contains("cursor=next") == true else {
                return (StubURLProtocol.response(to: request, status: 200), firstPage)
            }
            arrivedSignal.yield()
            for await _ in release { break }
            return (StubURLProtocol.response(to: request, status: 200), lastPage)
        }
        let model = MissingReceiptsModel()
        await model.load(year: 2026, api: api)
        let nextPage = Task { await model.loadMore(api: api) }
        for await _ in arrived { break }

        model.receiptAttached(ids[2])
        releaseSignal.yield()
        await nextPage.value

        #expect(model.items.map(\.id) == Array(ids[0..<2]))
        #expect(model.count == 2)
    }

    @Test func aFilterChangeForgetsAttachedRows() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let row = UUID()
        let other = UUID()
        let businessPage = missingPage([row], count: 1)
        let reimbursablePage = missingPage([other], count: 1)
        StubURLProtocol.respond { request in
            let isBusiness = request.url?.query()?.contains("bucket=business") == true
            return (StubURLProtocol.response(to: request, status: 200), isBusiness ? businessPage : reimbursablePage)
        }
        let model = MissingReceiptsModel()
        await model.load(year: 2026, api: api)
        model.receiptAttached(row)

        model.bucket = .reimbursable
        await model.load(year: 2026, api: api)
        model.bucket = .business
        await model.load(year: 2026, api: api)

        #expect(model.items.map(\.id) == [row])
        #expect(model.count == 1)
    }

    @Test func aFilterChangeResetsCountAndDataBeforeItsResponse() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let (model, _) = try await loadedFixtureModel(api)
        let (arrived, arrivedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let businessPage = missingPage([UUID()], count: 1)
        StubURLProtocol.respond { request in
            arrivedSignal.yield()
            for await _ in release { break }
            return (StubURLProtocol.response(to: request, status: 200), businessPage)
        }

        model.bucket = .business
        let switched = Task { await model.load(year: 2026, api: api) }
        for await _ in arrived { break }

        #expect(model.items.isEmpty)
        #expect(model.count == 0)
        #expect(!model.hasData)
        releaseSignal.yield()
        await switched.value
        #expect(model.count == 1)
        #expect(model.hasData)
    }

    @Test func failedReceiptUploadKeepsTheRowAndCount() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let (model, ids) = try await loadedFixtureModel(api)
        let png = missingPNG()
        let envelope = #"{"error":{"code":"unsupported_media_type","message":"Not an image.","request_id":"r4"}}"#
        _ = reply(415, body: Data(envelope.utf8))
        let uploads = ReceiptUploads(doneDisplay: .seconds(60), onUploaded: { model.receiptAttached($0) })

        await uploads.attach(to: ids[1], name: "Autozone", api: api) { png }

        guard case .failed = uploads.uploads.first?.state else {
            Issue.record("expected a failed upload, got \(String(describing: uploads.uploads.first?.state))")
            return
        }
        #expect(model.items.map(\.id) == ids)
        #expect(model.count == 3)
    }
}
