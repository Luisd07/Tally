import SwiftUI

/// Where a month screen puts its `MonthSwitcher`.
enum MonthSwitcherPlacement {
    /// A strip below the navigation bar.
    case bar
    /// Inside the navigation bar.
    case toolbar
    /// In the screen's content, which draws its own `PeriodSwitcher`.
    case content
}

extension View {
    /// Makes this view a screen for one month: the switcher at `placement`, `load` run on every month change,
    /// on pull to refresh and when the app returns to the foreground. Next stops at `latest`.
    func monthScreen(
        month: Binding<MonthRef>,
        latest: MonthRef,
        placement: MonthSwitcherPlacement = .bar,
        load: @escaping () async -> Void
    ) -> some View {
        withMonthSwitcher(month: month, latest: latest, placement: placement)
            .task(id: month.wrappedValue) { await load() }
            .refreshable { await load() }
            .reloadOnReturnToForeground { await load() }
    }

    @ViewBuilder
    private func withMonthSwitcher(month: Binding<MonthRef>, latest: MonthRef, placement: MonthSwitcherPlacement) -> some View {
        switch placement {
        case .bar:
            safeAreaInset(edge: .top, spacing: 0) { MonthSwitcher(month: month, latest: latest).bar }
        case .toolbar:
            toolbar { MonthSwitcher(month: month, latest: latest).toolbar }
        case .content:
            self
        }
    }
}

/// A month screen's content: `data` once a load has succeeded, else the failure with a Retry button, else a spinner.
struct MonthContent<Data: View>: View {
    let hasData: Bool
    let error: APIError?
    let failedTitle: String
    let retry: () -> Void
    @ViewBuilder let data: () -> Data

    var body: some View {
        if hasData {
            data()
        } else if let error {
            LoadFailedView(title: failedTitle, error: error, retry: retry)
        } else {
            ProgressView().frame(maxHeight: .infinity)
        }
    }
}
