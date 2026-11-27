import SwiftUI

/// Previous and Next buttons around the month's title. Next stops at `latest`. Place the pieces as the
/// screen needs: `toolbar` for the navigation bar, `bar` for a strip of its own, or each piece alone.
struct MonthSwitcher {
    @Binding var month: MonthRef
    /// The last month Next can reach.
    let latest: MonthRef

    var previousButton: some View {
        Button("Previous month", systemImage: "chevron.left") { month = month.previous }
    }

    var title: some View {
        Text(month.title()).font(.headline)
    }

    var nextButton: some View {
        Button("Next month", systemImage: "chevron.right") { month = month.next }
            .disabled(!(month < latest))
    }

    /// Previous at the leading edge of the navigation bar, the title in the middle, Next at the trailing edge.
    @ToolbarContentBuilder
    var toolbar: some ToolbarContent {
        ToolbarItem(placement: .topBarLeading) { previousButton }
        ToolbarItem(placement: .principal) { title }
        ToolbarItem(placement: .topBarTrailing) { nextButton }
    }

    /// The three pieces in a row on a bar background, e.g. for a top safe-area inset.
    var bar: some View {
        HStack {
            previousButton
            Spacer()
            title
            Spacer()
            nextButton
        }
        .labelStyle(.iconOnly)
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(.bar)
    }
}
