import Foundation
import Testing
@testable import Tally

private let hertzDescription = "  HERTZ RENTAL 1234 "

/// A transaction as the server sends it, with only the fields a rule suggestion reads varied.
private func ruleTransactionJSON(
    id: UUID, categoryId: UUID? = nil, bucket: String = "personal", parentId: UUID? = nil, isSplit: Bool = false
) -> String {
    let category = categoryId.map { "\"\($0.uuidString.lowercased())\"" } ?? "null"
    let parent = parentId.map { "\"\($0.uuidString.lowercased())\"" } ?? "null"
    return """
    {"id":"\(id.uuidString.lowercased())","account_id":"265cd02e-4a77-51ea-ad3a-4a9fbc282065",\
    "posted_date":"2026-10-08","transacted_at":null,"amount":"-85.00","description_raw":"\(hertzDescription)",\
    "merchant":null,"category_id":\(category),"bucket":"\(bucket)","source":"simplefin","external_id":null,\
    "pending":false,"reviewed":false,"notes":null,"event_id":null,"parent_id":\(parent),"reimbursement_id":null,\
    "is_split":\(isSplit),"created_at":"2026-10-18T20:42:06Z","updated_at":"2026-10-18T20:42:06Z"}
    """
}

@MainActor
private func ruleTransaction(
    categoryId: UUID? = nil, bucket: String = "personal", parentId: UUID? = nil, isSplit: Bool = false
) throws -> Transaction {
    let json = ruleTransactionJSON(
        id: UUID(), categoryId: categoryId, bucket: bucket, parentId: parentId, isSplit: isSplit
    )
    return try JSONDecoder.api.decode(Transaction.self, from: Data(json.utf8))
}

/// A rule as the server sends it, with only the fields a placement or a summary reads varied.
@MainActor
private func rule(
    _ pattern: String, priority: Int = 10, active: Bool = true, categoryId: UUID? = nil, bucket: String? = "business"
) throws -> Rule {
    let category = categoryId.map { "\"\($0.uuidString.lowercased())\"" } ?? "null"
    let setBucket = bucket.map { "\"\($0)\"" } ?? "null"
    let json = """
    {"id":"\(UUID().uuidString.lowercased())","priority":\(priority),"match_field":"description",\
    "match_type":"contains","pattern":"\(pattern)","set_merchant":null,"set_category_id":\(category),\
    "set_bucket":\(setBucket),"active":\(active),"created_at":"2026-10-18T20:42:06Z",\
    "updated_at":"2026-10-18T20:42:06Z"}
    """
    return try JSONDecoder.api.decode(Rule.self, from: Data(json.utf8))
}

@MainActor
private func travelCategories() throws -> [Tally.Category] {
    let json = #"""
    [{"id":"12cf5e88-b23e-5894-8ef6-99bd31bec28d","name":"Travel","kind":"expense","tax_line":null,\#
    "deductible_pct":100,"monthly_budget":null,"created_at":"2026-10-18T20:42:06Z","updated_at":"2026-10-18T20:42:06Z"}]
    """#
    return try JSONDecoder.api.decode([Tally.Category].self, from: Data(json.utf8))
}

/// Pure rule values: no network, so they run in parallel.
@MainActor
struct RuleSuggestionTests {
    @Test func noSuggestionWhenNothingChanged() throws {
        let category = UUID()
        let before = try ruleTransaction(categoryId: category, bucket: "business")
        #expect(RuleSuggestion(before: before, categoryId: category, bucket: .business) == nil)
    }

    @Test func changedCategoryAloneSetsOnlyTheCategory() throws {
        let category = UUID()
        let before = try ruleTransaction()
        let suggestion = try #require(RuleSuggestion(before: before, categoryId: category, bucket: .personal))
        #expect(suggestion.setCategoryId == category)
        #expect(suggestion.setBucket == nil)
    }

    @Test func changedBucketAloneSetsOnlyTheBucket() throws {
        let category = UUID()
        let before = try ruleTransaction(categoryId: category)
        let suggestion = try #require(RuleSuggestion(before: before, categoryId: category, bucket: .reimbursable))
        #expect(suggestion.setCategoryId == nil)
        #expect(suggestion.setBucket == .reimbursable)
    }

    @Test func changedCategoryAndBucketSetBoth() throws {
        let category = UUID()
        let before = try ruleTransaction(categoryId: UUID())
        let suggestion = try #require(RuleSuggestion(before: before, categoryId: category, bucket: .business))
        #expect(suggestion.setCategoryId == category)
        #expect(suggestion.setBucket == .business)
    }

    @Test func clearingTheCategoryAloneOffersNoRule() throws {
        let before = try ruleTransaction(categoryId: UUID())
        #expect(RuleSuggestion(before: before, categoryId: nil, bucket: .personal) == nil)
    }

    @Test func noSuggestionForASplitPart() throws {
        let child = try ruleTransaction(parentId: UUID())
        #expect(RuleSuggestion(before: child, categoryId: UUID(), bucket: .business) == nil)
    }

    @Test func noSuggestionForASplitParent() throws {
        let parent = try ruleTransaction(isSplit: true)
        #expect(RuleSuggestion(before: parent, categoryId: UUID(), bucket: .business) == nil)
    }

    @Test func suggestionDefaultsToTheTrimmedDescriptionAndContains() throws {
        let suggestion = try #require(RuleSuggestion(before: ruleTransaction(), categoryId: nil, bucket: .business))
        #expect(suggestion.pattern == "HERTZ RENTAL 1234")
        #expect(suggestion.matchType == .contains)
        let draft = try suggestion.validated.get()
        #expect(draft.id == suggestion.id)
        #expect(draft.priority == 100)
        #expect(draft.matchField == .description)
        #expect(draft.active)
    }

    @Test func patternMustBeNonBlankAndAtMost200Characters() throws {
        var suggestion = try #require(RuleSuggestion(before: ruleTransaction(), categoryId: nil, bucket: .business))
        suggestion.pattern = "   "
        #expect(suggestion.validated == .failure(.blank))
        suggestion.pattern = String(repeating: "a", count: 201)
        #expect(suggestion.validated == .failure(.tooLong))
        #expect(suggestion.validated.invalidMessage == "The pattern can't be over 200 characters.")
        suggestion.pattern = " " + String(repeating: "a", count: 200) + " "
        #expect(try suggestion.validated.get().pattern == String(repeating: "a", count: 200))
    }

    /// The server counts a pattern in code points (Python's `len`), not in what Swift counts as characters.
    @Test func patternLengthIsCountedInUnicodeScalars() throws {
        var suggestion = try #require(RuleSuggestion(before: ruleTransaction(), categoryId: nil, bucket: .business))
        suggestion.pattern = String(repeating: "e\u{301}", count: 199)
        #expect(suggestion.pattern.count == 199)
        #expect(suggestion.validated == .failure(.tooLong))
        suggestion.pattern = String(repeating: "e\u{301}", count: 100)
        #expect(try suggestion.validated.get().pattern.unicodeScalars.count == 200)
    }

    @Test func sentenceNamesThePatternAndWhatTheRuleSets() throws {
        let travel = try travelCategories()
        let category = travel[0].id
        var suggestion = try #require(
            RuleSuggestion(before: ruleTransaction(), categoryId: category, bucket: .reimbursable)
        )
        #expect(
            suggestion.sentence(categories: travel)
                == "Always file transactions containing “HERTZ RENTAL 1234” as Travel · Reimbursable"
        )
        suggestion.matchType = .startsWith
        suggestion.pattern = "HERTZ"
        #expect(
            suggestion.sentence(categories: []) == "Always file transactions starting with “HERTZ” as … · Reimbursable"
        )
    }

    @Test func ruleWhoseCategoryIsGoneSaysDeletedCategory() throws {
        let travel = try travelCategories()
        let gone = try rule("HERTZ", categoryId: UUID())
        #expect(gone.targetSummary(categories: travel) == "→ Deleted category · Business")
        #expect(try rule("HERTZ", bucket: nil).targetSummary(categories: travel) == "→ Deleted category")
        #expect(try rule("HERTZ", categoryId: travel[0].id).targetSummary(categories: travel) == "→ Travel · Business")
    }

    @Test func rulePromptHoldsAnOfferUntilTheEditorCloses() throws {
        let suggestion = try #require(RuleSuggestion(before: ruleTransaction(), categoryId: nil, bucket: .business))
        let prompt = RulePrompt()
        prompt.editorSaved()
        prompt.offer(suggestion)
        #expect(prompt.suggestion == nil)
        prompt.editorDismissed()
        #expect(prompt.suggestion == suggestion)
    }

    @Test func rulePromptShowsAnOfferAtOnceOnceClosed() throws {
        let suggestion = try #require(RuleSuggestion(before: ruleTransaction(), categoryId: nil, bucket: .business))
        let prompt = RulePrompt()
        prompt.editorSaved()
        prompt.editorDismissed()
        prompt.offer(suggestion)
        #expect(prompt.suggestion == suggestion)
    }

    @Test func rulePromptQueuesAnOfferWhileAnotherSheetIsUp() throws {
        let suggestion = try #require(RuleSuggestion(before: ruleTransaction(), categoryId: nil, bucket: .business))
        let prompt = RulePrompt()
        prompt.editorSaved()
        prompt.editorDismissed()
        prompt.setSheetPresented(true)
        prompt.offer(suggestion)
        #expect(prompt.suggestion == nil)
        prompt.setSheetPresented(false)
        #expect(prompt.suggestion == suggestion)
    }

    @Test func rulePromptDropsAQueuedOfferWhenTheNextEditorSaves() throws {
        let first = try #require(RuleSuggestion(before: ruleTransaction(), categoryId: nil, bucket: .business))
        let prompt = RulePrompt()
        prompt.setSheetPresented(true)
        prompt.offer(first)
        prompt.editorSaved()
        prompt.setSheetPresented(false)
        prompt.editorDismissed()
        #expect(prompt.suggestion == nil)
    }
}

/// Where a correction rule runs: pure, so they run in parallel.
@MainActor
struct RulePlacementTests {
    @Test func noOverlapGetsTheUsualPriority() throws {
        let placement = RulePlacement(pattern: "AMAZON PRIME VIDEO", rules: [try rule("HERTZ", priority: 10)])
        #expect(placement.priority == 100)
        #expect(placement.note == nil)
    }

    @Test func runsJustAheadOfTheBroaderRule() throws {
        let placement = RulePlacement(pattern: "AMAZON PRIME VIDEO", rules: [try rule("AMAZON", priority: 10)])
        #expect(placement.priority == 9)
        #expect(placement.note == nil)
    }

    @Test func runsAheadOfTheEarliestOfSeveralBroaderRules() throws {
        let rules = try [rule("AMAZON", priority: 40), rule("PRIME", priority: 20), rule("NETFLIX", priority: 5)]
        #expect(RulePlacement(pattern: "AMAZON PRIME VIDEO", rules: rules).priority == 19)
    }

    /// Priority can't go below 0, so the new rule ties with the broader one, which is older and wins. The tie is
    /// accepted and the sheet says so; the existing rule isn't changed.
    @Test func aBroaderRuleAtZeroTiesAndTheNoteSaysItWins() throws {
        let placement = RulePlacement(pattern: "AMAZON PRIME VIDEO", rules: [try rule("AMAZON", priority: 0)])
        #expect(placement.priority == 0)
        #expect(placement.stillAhead == ["AMAZON"])
        #expect(placement.note == "“AMAZON” also matches and was saved first, so it still wins.")
    }

    @Test func inactiveRulesDontCount() throws {
        let placement = RulePlacement(pattern: "AMAZON PRIME VIDEO", rules: [try rule("AMAZON", active: false)])
        #expect(placement.priority == 100)
    }

    @Test func aNarrowerRuleDoesntCount() throws {
        let placement = RulePlacement(pattern: "AMAZON", rules: [try rule("AMAZON PRIME", priority: 10)])
        #expect(placement.priority == 100)
    }

    /// Mirrors `normalize_text` in api/app/services/normalize.py: whitespace runs collapse, the ends are trimmed,
    /// and case is ignored, on both the new pattern and the rule's.
    @Test func overlapIsComparedOnTheServersNormalizedText() throws {
        #expect("  amazon\t\n prime   video ".ruleNormalized == "AMAZON PRIME VIDEO")
        let rules = [try rule(" Amazon  Prime ", priority: 30)]
        #expect(RulePlacement(pattern: "amazon   prime video", rules: rules).priority == 29)
    }

    @Test func uncheckedRulesGetTheUsualPriorityAndSaySo() {
        #expect(RulePlacement.unchecked.priority == 100)
        #expect(RulePlacement.unchecked.note?.hasPrefix("Couldn't check your other rules") == true)
    }
}

/// Lives in `APIClientTests`' serialized suite because the network tests share `StubURLProtocol`'s one handler.
extension APIClientTests {
    private func ruleJSON(
        id: UUID, pattern: String = "HERTZ", matchType: String = "contains", active: Bool = true
    ) -> String {
        """
        {"id":"\(uuidString(id))","priority":10,"match_field":"description","match_type":"\(matchType)",\
        "pattern":"\(pattern)","set_merchant":null,"set_category_id":null,"set_bucket":"business",\
        "active":\(active),"created_at":"2026-10-18T20:42:06Z","updated_at":"2026-10-18T20:42:06Z"}
        """
    }

    private func loadedRulesModel(_ rows: [String]) async -> (RulesModel, APIClient) {
        let api = makeClient(fixture.make())
        let model = RulesModel()
        _ = reply(200, body: Data("[\(rows.joined(separator: ","))]".utf8))
        await model.load(api: api)
        return (model, api)
    }

    private func ruleRequestJSON(_ request: URLRequest) throws -> [String: Any] {
        let body = try #require(bodyData(request))
        return try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
    }

    private var ruleNotFound: Data {
        Data(#"{"error":{"code":"not_found","message":"Rule not found.","request_id":"r1"}}"#.utf8)
    }

    // MARK: Models

    @Test func rulesFixtureDecodes() throws {
        let rules = try JSONDecoder.api.decode([Rule].self, from: jsonFixture("rules-list"))
        #expect(rules.count == 1)
        let rule = rules[0]
        #expect(rule.priority == 10)
        #expect(rule.matchField == .description)
        #expect(rule.matchType == .contains)
        #expect(rule.pattern == "HERTZ")
        #expect(rule.setMerchant == "Hertz")
        #expect(rule.setCategoryId == UUID(uuidString: "12cf5e88-b23e-5894-8ef6-99bd31bec28d"))
        #expect(rule.setBucket == .reimbursable)
        #expect(rule.active)
        #expect(rule.targetSummary(categories: []) == "→ Hertz · … · Reimbursable")
    }

    @Test func unknownMatchTypeDecodesAsOther() throws {
        let json = ruleJSON(id: UUID()).replacingOccurrences(of: #""contains""#, with: #""regex""#)
        let rule = try JSONDecoder.api.decode(Rule.self, from: Data(json.utf8))
        #expect(rule.matchType == .other("regex"))
        #expect(rule.targetSummary(categories: []) == "→ Business")
    }

    @Test func rulePatchSendsOnlySetKeys() throws {
        #expect(try snakeCaseKeys(RulePatch()).isEmpty)
        #expect(try snakeCaseKeys(RulePatch(active: false)) as NSDictionary == ["active": false] as NSDictionary)
        let patch = RulePatch(pattern: "DELTA", matchType: .startsWith)
        let keys = try snakeCaseKeys(patch) as NSDictionary
        #expect(keys == ["pattern": "DELTA", "match_type": "starts_with"] as NSDictionary)
    }

    // MARK: Create

    @Test func createRuleSendsSnakeCaseKeysAndOnlyTheChangedTarget() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let category = UUID()
        var suggestion = try #require(
            RuleSuggestion(before: ruleTransaction(), categoryId: category, bucket: .personal)
        )
        suggestion.matchType = .startsWith
        let created = ruleJSON(id: suggestion.id, pattern: "HERTZ RENTAL 1234", matchType: "starts_with")
        var requests = reply(201, body: Data(created.utf8)).makeAsyncIterator()

        let saved = await SaveRuleModel().create(try suggestion.validated.get(), api: api)
        let request = try #require(await requests.next())
        #expect(saved)
        #expect(request.httpMethod == "POST")
        #expect(request.url?.path() == "/api/rules")
        let json = try ruleRequestJSON(request)
        let keys: Set = ["id", "priority", "match_field", "match_type", "pattern", "set_category_id", "active"]
        #expect(Set(json.keys) == keys)
        #expect((json["id"] as? String)?.lowercased() == uuidString(suggestion.id))
        #expect(json["priority"] as? Int == 100)
        #expect(json["match_field"] as? String == "description")
        #expect(json["match_type"] as? String == "starts_with")
        #expect(json["pattern"] as? String == "HERTZ RENTAL 1234")
        #expect((json["set_category_id"] as? String)?.lowercased() == uuidString(category))
        #expect(json["active"] as? Bool == true)
    }

    @Test func createRuleSendsOnlyTheBucketWhenOnlyItChanged() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let suggestion = try #require(RuleSuggestion(before: ruleTransaction(), categoryId: nil, bucket: .business))
        var requests = reply(201, body: Data(ruleJSON(id: suggestion.id).utf8)).makeAsyncIterator()

        _ = try await api.createRule(suggestion.validated.get())
        let json = try ruleRequestJSON(try #require(await requests.next()))
        #expect(json["set_bucket"] as? String == "business")
        #expect(json["set_category_id"] == nil)
    }

    @Test func failedRuleCreateShowsTheMessageAndKeepsTheDraftId() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = SaveRuleModel()
        let suggestion = try #require(RuleSuggestion(before: ruleTransaction(), categoryId: nil, bucket: .business))
        let error = #"{"error":{"code":"validation_error","message":"pattern must not be blank","request_id":"r2"}}"#
        var failed = reply(422, body: Data(error.utf8)).makeAsyncIterator()

        #expect(await !model.create(try suggestion.validated.get(), api: api))
        #expect(model.createError?.errorDescription == "pattern must not be blank")
        let first = try ruleRequestJSON(try #require(await failed.next()))

        let created = ruleJSON(id: suggestion.id, pattern: "HERTZ RENTAL 1234")
        var retried = reply(201, body: Data(created.utf8)).makeAsyncIterator()
        #expect(await model.create(try suggestion.validated.get(), api: api))
        #expect(model.createError == nil)
        let second = try ruleRequestJSON(try #require(await retried.next()))
        #expect(first["id"] as? String == second["id"] as? String)
        #expect((second["id"] as? String)?.lowercased() == uuidString(suggestion.id))
    }

    /// A create replaying the draft's id answers with the rule as first saved; an edit made before the retry is
    /// then sent as a PATCH, and Save reports success only after it.
    @Test func replayedCreatePatchesThePatternAndMatchTypeEditedBeforeTheRetry() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        var suggestion = try #require(RuleSuggestion(before: ruleTransaction(), categoryId: nil, bucket: .business))
        suggestion.pattern = "HERTZ"
        suggestion.matchType = .startsWith
        let draft = try suggestion.validated.get()
        let replayed = Data(ruleJSON(id: draft.id, pattern: "HERTZ RENTAL 1234").utf8)
        let patched = Data(ruleJSON(id: draft.id, pattern: "HERTZ", matchType: "starts_with").utf8)
        let (requests, seen) = AsyncStream.makeStream(of: URLRequest.self)
        StubURLProtocol.respond { request in
            seen.yield(request)
            let body = request.httpMethod == "POST" ? replayed : patched
            return (StubURLProtocol.response(to: request, status: 200), body)
        }
        var iterator = requests.makeAsyncIterator()

        #expect(await SaveRuleModel().create(draft, api: api))
        let create = try #require(await iterator.next())
        #expect(create.httpMethod == "POST")
        let patch = try #require(await iterator.next())
        #expect(patch.httpMethod == "PATCH")
        #expect(patch.url?.path() == "/api/rules/\(uuidString(draft.id))")
        let json = try ruleRequestJSON(patch) as NSDictionary
        #expect(json == ["pattern": "HERTZ", "match_type": "starts_with"] as NSDictionary)
    }

    @Test func failedPatchAfterAReplayedCreateIsAFailure() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        var suggestion = try #require(RuleSuggestion(before: ruleTransaction(), categoryId: nil, bucket: .business))
        suggestion.pattern = "HERTZ"
        let draft = try suggestion.validated.get()
        let replayed = Data(ruleJSON(id: draft.id, pattern: "HERTZ RENTAL 1234").utf8)
        let notFound = ruleNotFound
        StubURLProtocol.respond { request in
            request.httpMethod == "POST"
                ? (StubURLProtocol.response(to: request, status: 200), replayed)
                : (StubURLProtocol.response(to: request, status: 404), notFound)
        }
        let model = SaveRuleModel()
        #expect(await !model.create(draft, api: api))
        #expect(model.createError?.errorDescription == "Rule not found.")
    }

    @Test func saveRuleModelPlacesTheRuleAmongTheLoadedRules() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = SaveRuleModel()
        #expect(model.placement(for: "HERTZ RENTAL") == nil)
        _ = reply(200, body: Data("[\(ruleJSON(id: UUID(), pattern: "HERTZ"))]".utf8))
        await model.loadRules(api: api)
        #expect(model.placement(for: "HERTZ RENTAL")?.priority == 9)
    }

    @Test func saveRuleModelFallsBackToTheUsualPriorityWhenRulesFailToLoad() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let model = SaveRuleModel()
        _ = reply(500, body: Data(#"{"error":{"code":"internal","message":"Boom.","request_id":"r5"}}"#.utf8))
        await model.loadRules(api: api)
        #expect(model.placement(for: "HERTZ RENTAL") == .unchecked)
    }

    // MARK: Rules screen

    @Test func rulesListInServerOrder() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<3).map { _ in UUID() }
        let (model, _) = await loadedRulesModel(ids.map { ruleJSON(id: $0) })
        #expect(model.rules.map(\.id) == ids)
        #expect(model.hasData)
    }

    @Test func togglingARulePatchesActiveAndKeepsItListed() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<2).map { _ in UUID() }
        let (model, api) = await loadedRulesModel(ids.map { ruleJSON(id: $0) })

        var requests = reply(200, body: Data(ruleJSON(id: ids[0], active: false).utf8)).makeAsyncIterator()
        await model.setActive(model.rules[0], to: false, api: api)
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "PATCH")
        #expect(request.url?.path() == "/api/rules/\(uuidString(ids[0]))")
        #expect(try ruleRequestJSON(request) as NSDictionary == ["active": false] as NSDictionary)
        #expect(model.rules.map(\.id) == ids)
        #expect(!model.rules[0].active)
        #expect(model.editError == nil)
    }

    @Test func failedToggleRollsBackAndSaysWhy() async throws {
        defer { try? fixture.cleanUp() }
        let id = UUID()
        let (model, api) = await loadedRulesModel([ruleJSON(id: id)])

        _ = reply(404, body: ruleNotFound)
        await model.setActive(model.rules[0], to: false, api: api)
        #expect(model.rules[0].active)
        #expect(model.editError?.message == "HERTZ: Rule not found.")
    }

    @Test func deletingARuleRemovesIt() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<2).map { _ in UUID() }
        let (model, api) = await loadedRulesModel(ids.map { ruleJSON(id: $0) })

        var requests = reply(204).makeAsyncIterator()
        await model.delete(model.rules[0], api: api)
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "DELETE")
        #expect(request.url?.path() == "/api/rules/\(uuidString(ids[0]))")
        #expect(bodyData(request)?.isEmpty ?? true)
        #expect(model.rules.map(\.id) == [ids[1]])
        #expect(model.editError == nil)
    }

    @Test func failedDeletePutsTheRuleBackInPlace() async throws {
        defer { try? fixture.cleanUp() }
        let ids = (0..<3).map { _ in UUID() }
        let rows = ids.map { ruleJSON(id: $0, pattern: "P\($0.uuidString.prefix(4))") }
        let (model, api) = await loadedRulesModel(rows)

        _ = reply(500, body: Data(#"{"error":{"code":"internal","message":"Boom.","request_id":"r3"}}"#.utf8))
        await model.delete(model.rules[1], api: api)
        #expect(model.rules.map(\.id) == ids)
        #expect(model.editError?.error.errorDescription == "Boom.")
    }

    // MARK: Prompt hook

    private func inboxPage(_ transactions: [String]) -> Data {
        let items = transactions.map {
            String($0.dropLast()) + #","needs_resplit":false,"unmatched_manual":false,"#
                + #""suggested_category_id":null,"suggested_bucket":null}"#
        }
        return Data(#"{"items":[\#(items.joined(separator: ","))],"next_cursor":null,"total":\#(items.count)}"#.utf8)
    }

    @Test func inboxRecategorizeOffersARuleOnlyAfterItSaved() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let ids = (0..<2).map { _ in UUID() }
        let model = InboxModel()
        _ = reply(200, body: inboxPage(ids.map { ruleTransactionJSON(id: $0) }))
        await model.load(api: api)

        _ = reply(200, body: Data(ruleTransactionJSON(id: ids[0], bucket: "business").utf8))
        let offered = await model.recategorize(model.items[0], categoryId: nil, bucket: .business, api: api)
        #expect(offered?.setBucket == .business)
        #expect(offered?.pattern == "HERTZ RENTAL 1234")

        _ = reply(500, body: Data(#"{"error":{"code":"internal","message":"Boom.","request_id":"r4"}}"#.utf8))
        #expect(await model.recategorize(model.items[0], categoryId: nil, bucket: .business, api: api) == nil)
        #expect(model.editError != nil)
    }

    @Test func transactionRecategorizeOffersARuleOnlyAfterItSaved() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let id = UUID()
        let category = UUID()
        let model = TransactionsModel()
        _ = reply(200, body: Data(#"{"items":[\#(ruleTransactionJSON(id: id))],"next_cursor":null}"#.utf8))
        await model.load(api: api)

        _ = reply(200, body: Data(ruleTransactionJSON(id: id, categoryId: category).utf8))
        let offered = await model.recategorize(model.items[0], categoryId: category, bucket: .personal, api: api)
        #expect(offered?.setCategoryId == category)
        #expect(offered != nil && offered?.setBucket == nil)

        #expect(await model.recategorize(model.items[0], categoryId: category, bucket: .personal, api: api) == nil)

        _ = reply(404, body: ruleNotFound)
        #expect(await model.recategorize(model.items[0], categoryId: nil, bucket: .business, api: api) == nil)
        #expect(model.items[0].bucket == .personal)
    }

    /// A bulk category edit saves without offering a rule: it returns nothing a prompt could show, so wiring one to
    /// it would have to change this test.
    @Test func bulkCategoryEditOffersNoRule() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let id = UUID()
        let category = UUID()
        let model = TransactionsModel()
        _ = reply(200, body: Data(#"{"items":[\#(ruleTransactionJSON(id: id))],"next_cursor":null}"#.utf8))
        await model.load(api: api)
        model.selectAllLoaded()

        let saved = ruleTransactionJSON(id: id, categoryId: category)
        var requests = reply(200, body: Data(#"{"updated":1,"items":[\#(saved)]}"#.utf8)).makeAsyncIterator()
        let _: Void = await model.bulkSetCategory(category, api: api)
        #expect(try #require(await requests.next()).url?.path() == "/api/transactions/bulk")
        #expect(model.items[0].categoryId == category)
    }
}
