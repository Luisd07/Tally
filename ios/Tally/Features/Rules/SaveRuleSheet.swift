import Observation
import SwiftUI

/// Offers to save `suggestion` as a rule after a correction. It loads the other rules first, so the new one runs
/// ahead of broader ones, and says when it can't. Save waits for the server; a failure shows inline and keeps the
/// suggestion's id for the retry. Not now closes it without a request.
struct SaveRuleSheet: View {
    let categories: [Category]

    @Environment(APIClient.self) private var api
    @State private var suggestion: RuleSuggestion
    @State private var model = SaveRuleModel()

    init(suggestion: RuleSuggestion, categories: [Category]) {
        self.categories = categories
        _suggestion = State(initialValue: suggestion)
    }

    var body: some View {
        let invalidMessage = suggestion.validated.invalidMessage
        let placement = model.placement(for: suggestion.pattern)
        FormSheet(
            title: "Save as rule",
            canSave: invalidMessage == nil && placement != nil,
            invalidReason: invalidMessage ?? (placement == nil ? "Checking your other rules…" : nil),
            // The draft carries the suggestion's id rather than the sheet's draft id: both last as long as the
            // sheet, and the suggestion's is the one the sheet is presented by.
            save: .waitsForServer({ _ in await save() }, failure: { model.createError }),
            saveTitle: "Save rule",
            dismissButton: .belowSave("Not now")
        ) {
            Section {
                Text(suggestion.sentence(categories: categories))
                if let note = placement?.note {
                    Text(note).font(.footnote).foregroundStyle(.secondary)
                }
            }
            Section {
                TextField("Pattern", text: $suggestion.pattern)
                    .textInputAutocapitalization(.characters)
                    .autocorrectionDisabled()
                Picker("Match", selection: $suggestion.matchType) {
                    ForEach(MatchType.pickerOptions(including: suggestion.matchType), id: \.self) {
                        Text($0.label).tag($0)
                    }
                }
            }
        }
        .presentationDetents([.medium, .large])
        .task { await model.loadRules(api: api) }
    }

    private func save() async -> Bool {
        guard case .success(var draft) = suggestion.validated,
              let placement = model.placement(for: draft.pattern)
        else { return false }
        draft.priority = placement.priority
        return await model.create(draft, api: api)
    }
}

/// Saves a suggested rule from the prompt after a correction, placed among the rules it joins.
@Observable
final class SaveRuleModel {
    /// The latest failed create; the sheet still holds the suggestion.
    private(set) var createError: APIError?
    private var rules = RulesCheck.loading

    private enum RulesCheck {
        case loading, loaded([Rule]), failed
    }

    /// Loads the rules a new one joins. A failed load leaves the usual priority; a cancelled one changes nothing.
    func loadRules(api: APIClient) async {
        do {
            rules = .loaded(try await api.rules())
        } catch is CancellationError {
            return
        } catch {
            rules = .failed
        }
    }

    /// Where a rule with `pattern` runs, by `RulePlacement`; nil until `loadRules` has answered.
    func placement(for pattern: String) -> RulePlacement? {
        switch rules {
        case .loading: nil
        case .loaded(let rules): RulePlacement(pattern: pattern, rules: rules)
        case .failed: .unchecked
        }
    }

    /// Creates `draft`. A create that replays the draft's id returns the rule as first saved, so when its pattern or
    /// match type differs from the draft's (edited before a retry), it is patched to the draft's before this reports
    /// success. Returns false on failure, with `createError` set unless it was cancelled; the draft keeps its id, so
    /// sending it again can't create a second rule.
    func create(_ draft: RuleDraft, api: APIClient) async -> Bool {
        createError = nil
        do {
            let saved = try await api.createRule(draft)
            let patch = RulePatch(bringing: saved, to: draft)
            if patch != RulePatch() { _ = try await api.updateRule(id: saved.id, patch: patch) }
            return true
        } catch is CancellationError {
            return false
        } catch {
            createError = APIError(wrapping: error)
            return false
        }
    }
}

/// When the "Save as rule" sheet shows: once no other sheet is up, since one sheet can't open over another or while
/// it is closing. A suggestion that arrives while the editor is closing or open again, or while another sheet the
/// screen names is up, waits and shows when that closes. The editor's next Save drops it for that correction's.
@Observable
final class RulePrompt {
    /// The suggestion shown as the sheet; the sheet clears it when it closes.
    var suggestion: RuleSuggestion?
    /// The suggestion waiting for the other sheets to close.
    private var pending: RuleSuggestion?
    private var editorClosing = false
    private var sheetPresented = false

    /// Call when the editor's Save closes it.
    func editorSaved() {
        editorClosing = true
        pending = nil
    }

    /// Call when the editor has closed, saved or not.
    func editorDismissed() {
        editorClosing = false
        showPending()
    }

    /// Call when a sheet other than the prompt opens or closes: the editor, or another the screen shows.
    func setSheetPresented(_ presented: Bool) {
        sheetPresented = presented
        showPending()
    }

    /// Shows `suggestion` once no other sheet is up.
    func offer(_ suggestion: RuleSuggestion) {
        pending = suggestion
        showPending()
    }

    private func showPending() {
        guard !editorClosing, !sheetPresented, let pending else { return }
        suggestion = pending
        self.pending = nil
    }
}

extension View {
    /// Edits `item`'s category and bucket in `CategoryEditorSheet` and sends the correction with `recategorize`.
    /// The rule it returns is offered as the "Save as rule" sheet by `RulePrompt`'s rules, which wait for the editor
    /// and for `otherSheetPresented`, a sheet the screen shows itself. Setting `offer` offers a rule the same way.
    func offersRuleAfterRecategorize<Item: Identifiable>(
        _ item: Binding<Item?>,
        transaction: @escaping (Item) -> Transaction,
        categories: [Category],
        otherSheetPresented: Bool = false,
        offer: Binding<RuleSuggestion?> = .constant(nil),
        recategorize: @escaping (Item, UUID?, Bucket) async -> RuleSuggestion?
    ) -> some View {
        modifier(RuleOfferingEditor(
            item: item,
            transaction: transaction,
            categories: categories,
            otherSheetPresented: otherSheetPresented,
            offer: offer,
            recategorize: recategorize
        ))
    }
}

/// The editor sheet and the "Save as rule" sheet after it, in the order `RulePrompt` needs.
private struct RuleOfferingEditor<Item: Identifiable>: ViewModifier {
    @Binding var item: Item?
    let transaction: (Item) -> Transaction
    let categories: [Category]
    let otherSheetPresented: Bool
    /// A rule from outside the editor (confirming a suggestion); offered through the same prompt, then cleared.
    @Binding var offer: RuleSuggestion?
    let recategorize: (Item, UUID?, Bucket) async -> RuleSuggestion?

    @State private var prompt = RulePrompt()

    func body(content: Content) -> some View {
        content
            .sheet(item: $item, onDismiss: prompt.editorDismissed) { edited in
                CategoryEditorSheet(transaction: transaction(edited), categories: categories) { categoryId, bucket in
                    prompt.editorSaved()
                    Task {
                        if let suggestion = await recategorize(edited, categoryId, bucket) { prompt.offer(suggestion) }
                    }
                }
            }
            .onChange(of: item != nil || otherSheetPresented) { _, presented in prompt.setSheetPresented(presented) }
            .onChange(of: offer) { _, suggestion in
                guard let suggestion else { return }
                offer = nil
                prompt.offer(suggestion)
            }
            .sheet(item: $prompt.suggestion) { suggestion in
                SaveRuleSheet(suggestion: suggestion, categories: categories)
            }
    }
}
