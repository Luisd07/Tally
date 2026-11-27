import Foundation
import Testing
@testable import Tally

/// Lives in `APIClientTests`' serialized suite because the network tests share `StubURLProtocol`'s one handler.
extension APIClientTests {
    private func categoryRow(
        id: UUID = UUID(), name: String, kind: String = "expense", budget: String? = nil, archived: Bool = false
    ) -> String {
        let monthly = budget.map { "\"\($0)\"" } ?? "null"
        return """
        {"id":"\(uuidString(id))","name":"\(name)","kind":"\(kind)","tax_line":null,"deductible_pct":100,\
        "monthly_budget":\(monthly),"archived":\(archived),"created_at":"2026-01-01T00:00:00Z",\
        "updated_at":"2026-01-01T00:00:00Z"}
        """
    }

    private func loadedCategoriesModel(_ rows: [String]) async -> (CategoriesModel, APIClient) {
        let api = makeClient(fixture.make())
        _ = reply(200, body: Data("[\(rows.joined(separator: ","))]".utf8))
        let model = CategoriesModel()
        await model.load(api: api)
        return (model, api)
    }

    @Test func categoriesLoadWithArchivedIncludedAndGroupByKind() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let body = "[" + [
            categoryRow(name: "Rent", budget: "1500.00"),
            categoryRow(name: "Dining"),
            categoryRow(name: "Salary", kind: "income"),
            categoryRow(name: "Old gym", archived: true),
            categoryRow(name: "Old side gig", kind: "income", archived: true)
        ].joined(separator: ",") + "]"
        var requests = reply(200, body: Data(body.utf8)).makeAsyncIterator()
        let model = CategoriesModel()

        await model.load(api: api)
        let request = try #require(await requests.next())
        #expect(request.url?.query() == nil)
        #expect(model.expense.map(\.name) == ["Dining", "Rent"])
        #expect(model.expense.last?.monthlyBudget == money("1500.00"))
        #expect(model.income.map(\.name) == ["Salary"])
        #expect(model.archived.map(\.name) == ["Old gym", "Old side gig"])
    }

    @Test func renamingACategoryUpdatesTheList() async throws {
        defer { try? fixture.cleanUp() }
        let id = UUID()
        let (model, api) = await loadedCategoriesModel([categoryRow(id: id, name: "Dining"), categoryRow(name: "Rent")])
        var requests = reply(200, body: Data(categoryRow(id: id, name: "Eating out").utf8)).makeAsyncIterator()

        let saved = await model.update(try #require(model.expense.first), patch: CategoryPatch(name: "Eating out"), api: api)
        let request = try #require(await requests.next())
        #expect(saved)
        #expect(request.httpMethod == "PATCH")
        #expect(model.expense.map(\.name) == ["Eating out", "Rent"])
    }

    @Test func archivingAndRestoringMoveACategoryBetweenSections() async throws {
        defer { try? fixture.cleanUp() }
        let id = UUID()
        let (model, api) = await loadedCategoriesModel([categoryRow(id: id, name: "Gym")])
        _ = reply(200, body: Data(categoryRow(id: id, name: "Gym", archived: true).utf8))

        await model.setArchived(try #require(model.expense.first), to: true, api: api)
        #expect(model.expense.isEmpty)
        #expect(model.archived.map(\.name) == ["Gym"])

        _ = reply(200, body: Data(categoryRow(id: id, name: "Gym").utf8))
        await model.setArchived(try #require(model.archived.first), to: false, api: api)
        #expect(model.archived.isEmpty)
        #expect(model.expense.map(\.name) == ["Gym"])
    }

    @Test func aFailedCategoryUpdateKeepsTheListAndSetsTheError() async throws {
        defer { try? fixture.cleanUp() }
        let (model, api) = await loadedCategoriesModel([categoryRow(name: "Gym")])
        _ = reply(500, body: Data(#"{"error":{"code":"internal","message":"Boom."}}"#.utf8))

        let saved = await model.setArchived(try #require(model.expense.first), to: true, api: api)
        #expect(!saved)
        #expect(model.updateError?.localizedDescription == "Boom.")
        #expect(model.archived.isEmpty)
        #expect(model.expense.map(\.name) == ["Gym"])
    }

    @Test func aCategoryCreatedInAnEditorIsAddedToTheCallersList() throws {
        let created = try JSONDecoder.api.decode(
            Tally.Category.self, from: Data(categoryRow(name: "Pets", budget: "40.00").utf8)
        )
        let model = CategoriesModel()
        model.add(created)
        model.add(created)
        #expect(model.expense.map(\.name) == ["Pets"])

        var reference = [Tally.Category]()
        let callback: (Tally.Category) -> Void = { category in
            if !reference.contains(where: { $0.id == category.id }) { reference.append(category) }
        }
        callback(created)
        callback(created)
        #expect(reference.map(\.name) == ["Pets"])
    }
}
