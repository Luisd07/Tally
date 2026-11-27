import SwiftUI

/// A month screen that is a list: the `MonthSwitcher`, a spinner then the list once `state` has a value, the first
/// load's failure with Retry, and a "Couldn't refresh" section above `rows` when a later load fails.
struct MonthListScreen<Value, Rows: View>: View {
    let title: String
    let failedTitle: String
    @Binding var month: MonthRef
    let state: MonthValue<Value>
    var placement: MonthSwitcherPlacement = .bar
    let fetch: (MonthRef) async throws -> Value
    @ViewBuilder let rows: (Value) -> Rows

    private let currentMonth = MonthRef()

    var body: some View {
        MonthContent(
            hasData: state.hasData,
            error: state.error,
            failedTitle: failedTitle,
            retry: { Task { await load() } }
        ) {
            if let value = state.value { list(value) }
        }
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
        .monthScreen(month: $month, latest: currentMonth, placement: placement) { await load() }
    }

    private func list(_ value: Value) -> some View {
        List {
            if let error = state.error {
                RetrySection(title: "Couldn't refresh", error: error) { Task { await load() } }
            }
            rows(value)
        }
    }

    private func load() async {
        let month = month
        await state.load(month: month) { try await fetch(month) }
    }
}
