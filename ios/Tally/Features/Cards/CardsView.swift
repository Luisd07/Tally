import SwiftUI

struct CardsView: View {
    @Environment(APIClient.self) private var api
    @State private var model = CardsModel()
    @State private var sheet: CardSheet?

    var body: some View {
        content
            .navigationTitle("Cards")
            .navigationBarTitleDisplayMode(.inline)
            .task { await load() }
            .refreshable { await load() }
            .reloadOnReturnToForeground { await load() }
            .sheet(item: $sheet) { sheet in
                switch sheet {
                case .edit(let card): CardFormSheet(model: model, card: card)
                case .balance(let card): BalanceFormSheet(model: model, card: card)
                }
            }
            .editFailureAlert(model.editError ?? model.loanEditError) {
                model.clearEditError()
                model.clearLoanEditError()
            }
    }

    @ViewBuilder
    private var content: some View {
        if model.hasData {
            list
        } else if let error = model.error {
            LoadFailedView(title: "Couldn't load Cards", error: error) { Task { await load() } }
        } else {
            ProgressView().frame(maxHeight: .infinity)
        }
    }

    private var list: some View {
        List {
            if let error = model.error {
                RetrySection(title: "Couldn't refresh", error: error) { Task { await load() } }
            }
            if let summary = model.interestSummary?.text {
                Section { Text(summary) }
            }
            Section {
                NavigationLink { PayoffPlanView() } label: {
                    Label("Payoff plan", systemImage: "chart.line.downtrend.xyaxis")
                }
            }
            Section {
                Toggle("Show inactive", isOn: includeInactive)
            }
            Section {
                if model.cards.isEmpty {
                    Text("No cards").foregroundStyle(.secondary)
                }
                ForEach(model.cards) { card in cardRows(card) }
            }
            if !model.loans.isEmpty {
                Section("Loans") {
                    ForEach(model.loans) { loan in
                        Button { sheet = .edit(loan.asCard) } label: { LoanRow(loan: loan) }
                    }
                }
            }
        }
    }

    private var includeInactive: Binding<Bool> {
        Binding(
            get: { model.includeInactive },
            set: { value in Task { await model.setIncludeInactive(value, api: api) } }
        )
    }

    @ViewBuilder
    private func cardRows(_ card: CardSummary) -> some View {
        Button { sheet = .edit(card) } label: { CardRow(card: card) }
        NavigationLink { CardLogView(card: card) } label: {
            Label("Monthly log", systemImage: "list.bullet.rectangle")
        }
        if card.sourceHint == .manual {
            Button("Update balance", systemImage: "square.and.pencil") { sheet = .balance(card) }
        }
    }

    private func load() async {
        await model.load(api: api)
    }
}

private enum CardSheet: Identifiable {
    case edit(CardSummary)
    case balance(CardSummary)

    var id: String {
        switch self {
        case .edit(let card): "edit-\(card.id.uuidString)"
        case .balance(let card): "balance-\(card.id.uuidString)"
        }
    }
}
