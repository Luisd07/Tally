import Foundation
import Observation

/// Ask about your money (#174). The conversation lives in memory only: the server keeps the history it needs (for
/// 30 days) under `conversationId`, and nothing is written to the phone.
@Observable
final class ChatModel {
    struct Line: Identifiable, Equatable {
        enum Role { case you, tally }

        let id = UUID()
        let role: Role
        let text: String
        /// What the answer rests on, e.g. "Spending by category, 2026-09".
        var lookedUp: [String] = []
        /// Changes the assistant proposes with this answer; nothing applies until the user confirms.
        var actions: [AIAction] = []
        /// Why this question got no answer; the question stays in the list with a Retry.
        var failure: String?
    }

    static let cutBackQuestion = "Where can I cut back this month?"

    /// The cut-back question for the month being looked at.
    static func cutBackQuestion(for month: MonthRef, now: MonthRef = MonthRef()) -> String {
        month == now ? cutBackQuestion : "Where could I have cut back in \(month.title())?"
    }

    /// The questions offered on an empty chat.
    static let starterPrompts = [
        "What did I spend most on this month?",
        cutBackQuestion,
        "Set up categories for my recent charges",
        "Find subscriptions I might have forgotten",
    ]

    private(set) var lines: [Line] = []
    private(set) var isSending = false
    /// Proposed chat actions the server holds, newest first, including ones whose answer never arrived.
    private(set) var pendingActions: [AIAction] = []
    var draft = ""
    /// Actions with a confirm, dismiss or undo in flight.
    private(set) var busyActions: Set<UUID> = []
    /// The server's reason an action's last confirm, dismiss or undo was refused.
    private(set) var actionNotices: [UUID: String] = [:]
    /// Minted here, so the server keeps one history per chat on this phone.
    private(set) var conversationId = UUID()
    /// Whether the chat's opening question has been sent; it goes out once, and only a Retry sends it again.
    private var hasStarted = false

    /// Pending actions not already shown under an answer: the "Waiting for you" section.
    var waitingActions: [AIAction] {
        let shown = Set(lines.flatMap(\.actions).map(\.id))
        return pendingActions.filter { !shown.contains($0.id) }
    }

    var canSend: Bool { !isSending && draft.trimmedOrNil != nil }
    var canStartOver: Bool { !lines.isEmpty && !isSending && busyActions.isEmpty }

    init(lines: [Line] = []) {
        self.lines = lines
    }

    /// Loads the proposed chat actions; a failed load leaves the section as it was.
    func loadPending(api: APIClient) async {
        guard let proposed = try? await api.aiActions(status: .proposed) else { return }
        pendingActions = proposed.filter { $0.source == .chat }
    }

    func send(api: APIClient) async {
        guard let question = draft.trimmedOrNil, !isSending else { return }
        draft = ""
        await ask(question, api: api)
    }

    /// Sends one of the starter prompts as the question.
    func sendStarter(_ prompt: String, api: APIClient) async {
        guard !isSending else { return }
        await ask(prompt, api: api)
    }

    /// Sends the question the chat was opened with, at most once however often the view appears.
    func start(with question: String, api: APIClient) async {
        guard !hasStarted, lines.isEmpty, !isSending else { return }
        hasStarted = true
        await ask(question, api: api)
    }

    /// Asks the failed question again.
    func retry(api: APIClient) async {
        guard !isSending, let failed = lines.last(where: { $0.failure != nil }) else { return }
        await ask(failed.text, api: api)
    }

    private func ask(_ question: String, api: APIClient) async {
        isSending = true
        defer { isSending = false }
        lines.removeAll { $0.failure != nil }
        let asked = Line(role: .you, text: question)
        lines.append(asked)
        do {
            let reply = try await api.chat(conversationId: conversationId, message: question)
            conversationId = reply.conversationId
            lines.append(Line(role: .tally, text: reply.reply, lookedUp: reply.lookedUp, actions: reply.actions))
        } catch is CancellationError {
            if let index = lines.firstIndex(where: { $0.id == asked.id }) {
                lines[index].failure = "Interrupted before Tally answered."
            }
        } catch {
            let wrapped = APIError(wrapping: error)
            if let index = lines.firstIndex(where: { $0.id == asked.id }) {
                lines[index].failure = Self.failureMessage(for: wrapped)
            }
            if case .transport = wrapped { await loadPending(api: api) }
        }
    }

    private static func failureMessage(for error: APIError) -> String? {
        if case .transport(let urlError) = error, urlError.code == .timedOut {
            return "This is taking longer than usual. If Tally suggested changes, they'll appear under Waiting for you."
        }
        return error.errorDescription
    }

    func confirm(_ action: AIAction, api: APIClient, onEdited: (() -> Void)? = nil) async {
        if await run(action, api: api, call: api.confirmAIAction) { onEdited?() }
    }

    func dismiss(_ action: AIAction, api: APIClient) async {
        _ = await run(action, api: api, call: api.dismissAIAction)
    }

    func undo(_ action: AIAction, api: APIClient, onEdited: (() -> Void)? = nil) async {
        if await run(action, api: api, call: api.undoAIAction) { onEdited?() }
    }

    /// Runs one action call and shows its result on the card; a refusal keeps the server's message and, for a
    /// conflict, refetches the action so the card shows what the server now has. True when the server answered.
    private func run(_ action: AIAction, api: APIClient, call: (UUID) async throws -> AIAction) async -> Bool {
        guard busyActions.insert(action.id).inserted else { return false }
        defer { busyActions.remove(action.id) }
        actionNotices[action.id] = nil
        do {
            let updated = try await call(action.id)
            replace(updated)
            return true
        } catch is CancellationError {
            return false
        } catch {
            let wrapped = APIError(wrapping: error)
            actionNotices[action.id] = wrapped.errorDescription
            if case .server(let serverError) = wrapped, serverError.status == 409,
                let current = try? await api.aiAction(id: action.id)
            {
                replace(current)
            }
            return false
        }
    }

    private func replace(_ updated: AIAction) {
        if let at = pendingActions.firstIndex(where: { $0.id == updated.id }) {
            pendingActions[at] = updated
        }
        for index in lines.indices {
            if let at = lines[index].actions.firstIndex(where: { $0.id == updated.id }) {
                lines[index].actions[at] = updated
            }
        }
    }

    /// Forgets this chat on the phone and starts another; the server deletes the old one after 30 days.
    func startOver() {
        guard canStartOver else { return }
        lines = []
        hasStarted = true
        draft = ""
        busyActions = []
        actionNotices = [:]
        conversationId = UUID()
    }
}
