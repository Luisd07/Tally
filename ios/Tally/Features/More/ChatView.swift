import SwiftUI

/// Ask about your money (#174): questions answered from your own data by read-only lookups. Changes happen only
/// through action cards the user confirms.
struct ChatView: View {
    @Environment(APIClient.self) private var api
    @Environment(\.inboxEdited) private var inboxEdited
    @State private var model: ChatModel
    @State private var reference = ReferenceData()

    private static let endID = "end"

    /// Asked as soon as the chat opens, when it has no conversation yet.
    private let question: String?

    init(model: ChatModel = ChatModel(), question: String? = nil) {
        _model = State(initialValue: model)
        self.question = question
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(spacing: 0) {
                    waiting
                    if model.lines.isEmpty {
                        emptyState
                    } else {
                        messages
                    }
                }
            }
            .scrollDismissesKeyboard(.interactively)
            .onChange(of: model.lines.count) { scrollToEnd(proxy) }
            .onChange(of: model.isSending) { scrollToEnd(proxy) }
        }
        .safeAreaInset(edge: .bottom) { inputBar }
        .task { await reference.load(api: api) }
        .task { await model.loadPending(api: api) }
        .task {
            guard let question else { return }
            await model.start(with: question, api: api)
        }
        .navigationTitle("Ask Tally")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button("New chat", systemImage: "square.and.pencil") { model.startOver() }
                    .disabled(!model.canStartOver)
            }
        }
    }

    private func scrollToEnd(_ proxy: ScrollViewProxy) {
        withAnimation { proxy.scrollTo(Self.endID, anchor: .bottom) }
    }

    private var messages: some View {
        LazyVStack(spacing: 12) {
            ForEach(model.lines) { line in
                bubble(line)
                if let failure = line.failure {
                    failureRow(failure)
                }
                ForEach(line.actions) { actionCard($0) }
            }
            if model.isSending {
                ThinkingDots()
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Color.clear.frame(height: 1).id(Self.endID)
        }
        .padding(.horizontal)
        .padding(.top, 12)
    }

    /// Proposals from answers that never reached the phone (or an earlier visit), as the same cards.
    @ViewBuilder private var waiting: some View {
        if !model.waitingActions.isEmpty {
            VStack(alignment: .leading, spacing: 12) {
                Text("Waiting for you")
                    .font(.headline)
                    .accessibilityAddTraits(.isHeader)
                ForEach(model.waitingActions) { actionCard($0) }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal)
            .padding(.top, 12)
        }
    }

    private func actionCard(_ action: AIAction) -> some View {
        ActionCard(
            action: action,
            categories: reference.categories,
            isBusy: model.busyActions.contains(action.id),
            notice: model.actionNotices[action.id],
            onConfirm: { Task { await model.confirm(action, api: api, onEdited: inboxEdited) } },
            onDismiss: { Task { await model.dismiss(action, api: api) } },
            onUndo: { Task { await model.undo(action, api: api, onEdited: inboxEdited) } }
        )
    }

    private var emptyState: some View {
        VStack(spacing: 20) {
            VStack(spacing: 8) {
                Image(systemName: "sparkles")
                    .font(.largeTitle)
                    .foregroundStyle(.tint)
                    .accessibilityHidden(true)
                Text("Ask about your spending, income, bills or taxes.")
                    .font(.headline)
                    .multilineTextAlignment(.center)
            }
            VStack(spacing: 8) {
                ForEach(ChatModel.starterPrompts, id: \.self) { prompt in
                    Button {
                        Task { await model.sendStarter(prompt, api: api) }
                    } label: {
                        Text(prompt)
                            .font(.subheadline)
                            .multilineTextAlignment(.leading)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(12)
                            .background(Color(.secondarySystemBackground), in: .rect(cornerRadius: 12))
                            .contentShape(.rect(cornerRadius: 12))
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .padding(.horizontal)
        .padding(.top, 48)
    }

    private func bubble(_ line: ChatModel.Line) -> some View {
        let isYou = line.role == .you
        return VStack(alignment: isYou ? .trailing : .leading, spacing: 4) {
            Text(Self.markdown(line.text))
                .foregroundStyle(isYou ? Color.white : Color.primary)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(isYou ? Color.accentColor : Color(.secondarySystemBackground), in: .rect(cornerRadius: 18))
                .textSelection(.enabled)
            if !line.lookedUp.isEmpty {
                Text("Looked up " + line.lookedUp.joined(separator: " · "))
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 6)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel((isYou ? "You: " : "Tally: ") + String(Self.markdown(line.text).characters))
        .frame(maxWidth: .infinity, alignment: isYou ? .trailing : .leading)
        .padding(isYou ? .leading : .trailing, 48)
    }

    private func failureRow(_ message: String) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.circle")
                .foregroundStyle(.red)
                .accessibilityHidden(true)
            Text(message)
                .font(.footnote)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.trailing)
            Button("Retry") { Task { await model.retry(api: api) } }
                .font(.footnote.weight(.semibold))
                .disabled(model.isSending)
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .padding(.leading, 48)
    }

    private var inputBar: some View {
        HStack(alignment: .bottom, spacing: 8) {
            TextField("Ask a question", text: $model.draft, axis: .vertical)
                .lineLimit(1...5)
                .submitLabel(.send)
                .onSubmit { Task { await model.send(api: api) } }
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .background(Color(.secondarySystemBackground), in: .rect(cornerRadius: 20))
            Button("Send", systemImage: "arrow.up.circle.fill") { Task { await model.send(api: api) } }
                .labelStyle(.iconOnly)
                .font(.title)
                .disabled(!model.canSend)
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(.bar)
    }

    /// Bold and italics from the reply; anything else as plain text. Links are dropped (shown as their text): a reply
    /// can quote bank or merchant text, so a link in it could send data anywhere with one tap.
    static func markdown(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        guard var parsed = try? AttributedString(markdown: text, options: options) else { return AttributedString(text) }
        for run in parsed.runs where run.link != nil {
            parsed[run.range].link = nil
        }
        return parsed
    }
}

/// Three dots that pulse in turn while an answer is on its way.
private struct ThinkingDots: View {
    var body: some View {
        TimelineView(.animation) { context in
            let time = context.date.timeIntervalSinceReferenceDate
            HStack(spacing: 5) {
                ForEach(0..<3, id: \.self) { index in
                    Circle()
                        .fill(.secondary)
                        .frame(width: 8, height: 8)
                        .opacity(0.3 + 0.7 * max(0, sin(time * 5 - Double(index) * 0.8)))
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 16)
        .background(Color(.secondarySystemBackground), in: .rect(cornerRadius: 18))
        .accessibilityLabel("Tally is thinking")
    }
}

#if DEBUG
extension ChatModel {
    /// A short conversation for screenshots.
    static var sample: ChatModel {
        ChatModel(lines: [
            Line(role: .you, text: "What did I spend most on this month?"),
            Line(
                role: .tally,
                text: "Groceries is your biggest category so far: **$412.30** across 14 charges, ahead of Dining out at **$268.75**.",
                lookedUp: ["Spending by category, 2026-10"]
            ),
            Line(role: .you, text: "And how does that compare to September?"),
            Line(
                role: .tally,
                text: "Groceries is up about 8% from September's **$381.60**. Dining out is down **$41.20**.",
                lookedUp: ["Spending by category, 2026-09", "Spending by category, 2026-10"]
            ),
            Line(role: .you, text: "Find my gas charges", failure: "Couldn't reach the server."),
        ])
    }
}
#endif
