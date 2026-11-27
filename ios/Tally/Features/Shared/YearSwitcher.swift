import SwiftUI

/// Previous and Next buttons around the year, on a bar background, e.g. for a top safe-area inset.
/// The buttons stop at the ends of `range`.
struct YearSwitcher: View {
    @Binding var year: Int
    var range: ClosedRange<Int> = 2000...MonthRef().year

    var body: some View {
        HStack {
            Button("Previous year", systemImage: "chevron.left") { year -= 1 }
                .disabled(year <= range.lowerBound)
            Spacer()
            Text(String(year)).font(.headline)
            Spacer()
            Button("Next year", systemImage: "chevron.right") { year += 1 }
                .disabled(year >= range.upperBound)
        }
        .labelStyle(.iconOnly)
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(.bar)
    }
}
