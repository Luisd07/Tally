import Foundation
import os
import Testing
@testable import Tally

// MARK: - Network (shares StubURLProtocol, so these run in the serialized suite)

extension APIClientTests {
    private func transactionJSON(id: UUID) -> String {
        """
        {"id":"\(id.uuidString.lowercased())","account_id":"265cd02e-4a77-51ea-ad3a-4a9fbc282065","posted_date":"2026-10-22","transacted_at":null,"amount":"-21.60","description_raw":"CHIPOTLE 2291","merchant":null,"category_id":null,"bucket":"personal","source":"simplefin","external_id":null,"pending":false,"reviewed":false,"notes":null,"event_id":null,"parent_id":null,"reimbursement_id":null,"is_split":false,"created_at":"2026-10-22T20:42:06Z","updated_at":"2026-10-22T20:42:06Z"}
        """
    }

    @Test func receiptsDecodeWithTheirReadings() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let transactionId = UUID()
        let category = UUID()
        let body = """
        [{"id":"\(UUID().uuidString.lowercased())","transaction_id":"\(transactionId.uuidString.lowercased())","mime":"image/jpeg","sha256":"ab","uploaded_at":"2026-10-22T20:42:06Z",
          "reading":{"status":"ready","merchant":"Chipotle","receipt_date":"2026-10-22","subtotal":"20.00","tax":"1.60","tip":null,"total":"21.60",
                     "line_items":[{"description":"Bowl","amount":"12.50"}],"category_id":"\(category.uuidString.lowercased())","adds_up":false,"matches_transaction":true}},
         {"id":"\(UUID().uuidString.lowercased())","transaction_id":"\(transactionId.uuidString.lowercased())","mime":"image/jpeg","sha256":"cd","uploaded_at":"2026-10-23T20:42:06Z","reading":null}]
        """
        let requests = reply(200, body: Data(body.utf8))

        let receipts = try await makeClient(session).receipts(transactionId: transactionId)

        let reading = try #require(receipts.first?.reading)
        #expect(reading.status == .ready)
        #expect(reading.merchant == "Chipotle")
        #expect(reading.receiptDate == CalendarDate(year: 2026, month: 10, day: 22))
        #expect(reading.total == Money(Decimal(string: "21.60")!))
        #expect(reading.lineItems == [ReceiptReading.LineItem(description: "Bowl", amount: Money(Decimal(string: "12.50")!))])
        #expect(reading.categoryId == category)
        #expect(reading.addsUp == false)
        #expect(receipts.last?.reading == nil)
        var iterator = requests.makeAsyncIterator()
        let request = try #require(await iterator.next())
        #expect(request.url?.path() == "/api/transactions/\(transactionId.uuidString.lowercased())/receipts")
    }

    @Test func usingAReadingSetsMerchantAndCategory() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let api = makeClient(session)
        let id = UUID()
        let category = UUID()
        let model = TransactionsModel()
        _ = reply(200, body: Data(#"{"items":[\#(transactionJSON(id: id))],"next_cursor":null}"#.utf8))
        await model.load(api: api)
        let transaction = try #require(model.transaction(id: id))

        let reading = try JSONDecoder.api.decode(ReceiptReading.self, from: Data("""
        {"status":"ready","merchant":"Chipotle","receipt_date":null,"subtotal":null,"tax":null,"tip":null,"total":"21.60",
         "line_items":[],"category_id":"\(category.uuidString.lowercased())","adds_up":null,"matches_transaction":true}
        """.utf8))
        var requests = reply(200, body: Data(transactionJSON(id: id).utf8)).makeAsyncIterator()
        await model.useReceiptReading(transaction, reading, api: api)

        let body = try #require(await requests.next().flatMap(bodyData))
        let json = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
        #expect(Set(json.keys) == ["category_id", "merchant"])
        #expect(json["merchant"] as? String == "Chipotle")
        #expect((json["category_id"] as? String)?.lowercased() == category.uuidString.lowercased())
    }

    @Test func finishedUploadsAreCounted() async throws {
        defer { try? fixture.cleanUp() }
        let session = fixture.make()
        try session.signIn(token: "t1", serverURL: server)
        let transactionId = UUID()
        let reply = #"{"id":"\#(UUID().uuidString.lowercased())","transaction_id":"\#(transactionId.uuidString.lowercased())","mime":"image/jpeg","sha256":"ab","uploaded_at":"2026-10-22T20:42:06Z"}"#
        _ = self.reply(201, body: Data(reply.utf8))
        let uploads = ReceiptUploads(doneDisplay: .seconds(60))
        #expect(uploads.completedCount == 0)

        await uploads.attach(to: transactionId, name: "Chipotle", api: makeClient(session), scannedPages: [scanPage(width: 10, height: 10)])

        #expect(uploads.completedCount == 1)
    }
}
