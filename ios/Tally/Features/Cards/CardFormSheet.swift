import SwiftUI

/// Edits a card's (or loan's) due day, planned payment, APR and promo end. Only the fields that changed are sent; the sheet
/// closes at once and a failure shows as the screen's alert.
struct CardFormSheet: View {
    let model: CardsModel
    let card: CardSummary

    @Environment(APIClient.self) private var api
    @State private var fields: CardFields

    init(model: CardsModel, card: CardSummary) {
        self.model = model
        self.card = card
        _fields = State(initialValue: CardFields(editing: card))
    }

    private var form: Result<CardForm, CardForm.Invalid> { fields.validated }

    /// What Save sends: the fields that changed, or nil when the form is invalid or changes nothing.
    private var patch: AccountPatch? {
        guard case .success(let form) = form else { return nil }
        return AccountPatch(from: card, to: form)
    }

    var body: some View {
        FormSheet(
            title: card.account.name,
            canSave: patch != nil,
            invalidReason: form.invalidMessage,
            save: .closesAtOnce(update)
        ) {
            Toggle("This is a loan", isOn: $fields.isLoan)
            TextField("Due day (1-31)", text: $fields.dueDayText)
                .keyboardType(.numberPad)
            TextField("Planned payment", text: $fields.plannedPaymentText)
                .keyboardType(.decimalPad)
            TextField("APR %", text: $fields.aprText)
                .keyboardType(.decimalPad)
            Toggle("Promo APR end", isOn: $fields.hasPromoEnd)
            if fields.hasPromoEnd {
                DatePicker("Date", selection: $fields.promoAprEnd.asDate, displayedComponents: .date)
            }
        }
    }

    private func update() {
        guard let patch else { return }
        Task { await model.update(card, patch: patch, api: api) }
    }
}
