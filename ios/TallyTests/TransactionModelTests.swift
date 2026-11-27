import Foundation
import Testing
@testable import Tally

@MainActor
struct TransactionModelTests {
    private final class BundleToken {}

    private func fixture(_ name: String) throws -> Data {
        let url = try #require(Bundle(for: BundleToken.self).url(forResource: name, withExtension: "json"))
        return try Data(contentsOf: url)
    }

    private func encoded(_ patch: TransactionPatch) throws -> [String: Any] {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let object = try JSONSerialization.jsonObject(with: encoder.encode(patch))
        return try #require(object as? [String: Any])
    }

    @Test func inboxPageFixtureDecodes() throws {
        let page = try JSONDecoder.api.decode(InboxPage.self, from: fixture("inbox-page"))
        #expect(page.total == 12)
        #expect(page.items.count == 2)
        #expect(page.nextCursor != nil)
        let first = try #require(page.items.first)
        #expect(first.unmatchedManual)
        #expect(first.transaction.bucket == .reimbursable)
        #expect(first.transaction.source == .manual)
        #expect(first.transaction.amount == Money(Decimal(string: "-85.00")!))
        #expect(first.transaction.postedDate == CalendarDate(year: 2026, month: 10, day: 8))
        #expect(first.id == first.transaction.id)
        #expect(first.suggestedBucket == nil)
    }

    @Test func transactionsPageFixtureDecodes() throws {
        let page = try JSONDecoder.api.decode(TransactionPage.self, from: fixture("transactions-page"))
        #expect(!page.items.isEmpty)
    }

    @Test func categoriesFixtureDecodes() throws {
        let data = try fixture("categories")
        let categories = try JSONDecoder.api.decode([Tally.Category].self, from: data)
        #expect(categories.count == 4)
        let meals = try #require(categories.first(where: { $0.name == "Meals" }))
        #expect(meals.kind == .expense)
        #expect(meals.taxLine == "meals")
        #expect(meals.deductiblePct == 50)
        #expect(categories.contains(where: { $0.kind == .income }))
    }

    @Test func accountsFixtureDecodes() throws {
        let accounts = try JSONDecoder.api.decode([Account].self, from: fixture("accounts"))
        #expect(accounts.count == 2)
        let card = try #require(accounts.first { $0.kind == .creditCard })
        #expect(card.dueDay == 15)
        #expect(card.plannedPayment == Money(Decimal(string: "100.00")!))
        #expect(card.apr == "24.99")
    }

    @Test func unknownServerEnumsFallBack() throws {
        struct Holder: Decodable { let source: Source; let account: AccountKind; let category: CategoryKind }
        let json = #"{"source":"plaid","account":"brokerage","category":"transfer"}"#
        let holder = try JSONDecoder().decode(Holder.self, from: Data(json.utf8))
        #expect(holder.source == .other("plaid"))
        #expect(holder.account == .other("brokerage"))
        #expect(holder.category == .other("transfer"))
    }

    @Test func applyingSetsClearsAndLeavesFields() throws {
        let page = try JSONDecoder.api.decode(TransactionPage.self, from: fixture("transactions-page"))
        let original = try #require(page.items.first)
        let category = UUID()

        let set = original.applying(TransactionPatch(categoryId: .some(category), bucket: .income, notes: .some("Lunch"), reviewed: true))
        #expect(set.categoryId == category)
        #expect(set.bucket == .income)
        #expect(set.notes == "Lunch")
        #expect(set.reviewed)
        #expect(set.merchant == original.merchant)
        #expect(set.amount == original.amount)
        #expect(set.id == original.id)

        let cleared = set.applying(TransactionPatch(categoryId: .some(nil), notes: .some(nil), merchant: .some(nil)))
        #expect(cleared.categoryId == nil)
        #expect(cleared.notes == nil)
        #expect(cleared.merchant == nil)
        #expect(cleared.bucket == .income)
        #expect(cleared.reviewed)

        #expect(original.applying(TransactionPatch()) == original)
    }

    @Test func bucketWireStringsRoundTrip() {
        for bucket in Bucket.allKnown {
            #expect(Bucket.known[bucket.rawValue] == bucket)
        }
        #expect(Bucket.known.count == Bucket.allKnown.count)
        #expect(Bucket.allKnown.first == .personal)
    }

    @Test func patchEncodesOnlySetFields() throws {
        #expect(Set(try encoded(TransactionPatch(reviewed: true)).keys) == ["reviewed"])

        let id = UUID()
        let patch = TransactionPatch(categoryId: .some(id), bucket: .business, reviewed: true)
        let object = try encoded(patch)
        #expect(Set(object.keys) == ["category_id", "bucket", "reviewed"])
        #expect(object["category_id"] as? String == id.uuidString)
        #expect(object["bucket"] as? String == "business")
        #expect(object["reviewed"] as? Bool == true)
    }

    @Test func patchClearsWithExplicitNull() throws {
        let object = try encoded(TransactionPatch(categoryId: .some(nil)))
        #expect(Set(object.keys) == ["category_id"])
        #expect(object["category_id"] is NSNull)
    }

    @Test func emptyPatchEncodesEmptyObject() throws {
        #expect(try encoded(TransactionPatch()).isEmpty)
    }
}
