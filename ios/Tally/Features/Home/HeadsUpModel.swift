import Foundation
import Observation

/// Home's "Heads up" card (#225): the insights worth a look, what each proposed fix would change, and the
/// dismiss / confirm / not-now / undo taps on them. Nothing is stored; a failed load shows nothing new.
@Observable
final class HeadsUpModel {
    /// The insights still showing, newest first.
    private(set) var insights: [Insight] = []
    /// The proposed action behind each insight that has one, by insight id.
    private(set) var actions: [UUID: AIAction] = [:]
    /// Insights whose action was just applied, by insight id: shown as "Done" with Undo until `settle`.
    private(set) var done: [UUID: AIAction] = [:]
    /// Why the last tap on an insight failed, by insight id.
    private(set) var failures: [UUID: String] = [:]

    var isEmpty: Bool { insights.isEmpty }

    /// Loads the current insights, plus the action each proposes so the card can say what it would change.
    /// A failure keeps what is showing; this section never reports one.
    func load(api: APIClient) async {
        guard let loaded = try? await api.insights() else { return }
        let showing = loaded.filter { insight in
            switch insight.actionStatus {
            case .applied, .undone, .dismissed: done[insight.id] != nil
            default: true
            }
        }
        var loadedActions: [UUID: AIAction] = [:]
        for insight in showing where insight.actionStatus == .proposed {
            guard let id = insight.actionId, let action = try? await api.aiAction(id: id) else { continue }
            loadedActions[insight.id] = action
        }
        actions = loadedActions
        insights = showing
    }

    /// Whether `insight` has a proposed action the card can confirm: one whose changes it knows.
    func canConfirm(_ insight: Insight) -> Bool {
        insight.actionStatus == .proposed && actions[insight.id] != nil && done[insight.id] == nil
    }

    func dismiss(_ insight: Insight, api: APIClient) async {
        guard await dismissOnServer(insight, api: api) else { return }
        remove(insight)
    }

    /// Applies the insight's proposed action and keeps the row as "Done" until `settle`.
    func confirm(_ insight: Insight, api: APIClient) async {
        guard let id = insight.actionId, canConfirm(insight) else { return }
        do {
            done[insight.id] = try await api.confirmAIAction(id: id)
            failures[insight.id] = nil
        } catch {
            failures[insight.id] = APIError(wrapping: error).errorDescription
        }
    }

    /// Declines the proposed fix and dismisses the insight.
    func notNow(_ insight: Insight, api: APIClient) async {
        if let id = insight.actionId {
            do {
                _ = try await api.dismissAIAction(id: id)
            } catch {
                failures[insight.id] = APIError(wrapping: error).errorDescription
                return
            }
        }
        await dismiss(insight, api: api)
    }

    /// Reverts a just-applied action and dismisses the insight.
    func undo(_ insight: Insight, api: APIClient) async {
        guard let id = insight.actionId, done[insight.id] != nil else { return }
        do {
            _ = try await api.undoAIAction(id: id)
        } catch {
            failures[insight.id] = APIError(wrapping: error).errorDescription
            return
        }
        await dismiss(insight, api: api)
    }

    /// Ends the "Done" window: the insight is handled, so it is dismissed and leaves the card.
    func settle(_ insight: Insight, api: APIClient) async {
        guard done[insight.id] != nil else { return }
        _ = try? await api.dismissInsight(id: insight.id)
        remove(insight)
    }

    private func dismissOnServer(_ insight: Insight, api: APIClient) async -> Bool {
        do {
            _ = try await api.dismissInsight(id: insight.id)
            failures[insight.id] = nil
            return true
        } catch {
            failures[insight.id] = APIError(wrapping: error).errorDescription
            return false
        }
    }

    private func remove(_ insight: Insight) {
        insights.removeAll { $0.id == insight.id }
        actions[insight.id] = nil
        done[insight.id] = nil
    }
}
