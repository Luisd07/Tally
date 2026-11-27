import SwiftUI

/// The app's look: a layout direction and one accent. Release builds always use `standard`; DEBUG builds can pick
/// another at launch with `TALLY_THEME` and `TALLY_ACCENT`.
struct Theme: Equatable {
    /// A: rounded hero numbers, a summary card, category glyphs and roomy rows.
    /// B: SF Pro, dense rows, no glyphs and budget bars on Home.
    /// Mix: A's summary cards on B's dense rows, type and thin bars, with a Home built for the month at a glance.
    enum Direction: String {
        case a = "A", b = "B", mix = "MIX"
    }

    enum Accent: String {
        case blue, indigo

        var color: Color {
            switch self {
            case .blue: Color(.accent)
            case .indigo: .indigo
            }
        }
    }

    var direction: Direction
    var accent: Accent

    static let standard = Theme(direction: .mix, accent: .blue)

    /// The hero number at the top of a screen.
    var heroFont: Font {
        switch direction {
        case .a: .system(.largeTitle, design: .rounded).bold().monospacedDigit()
        case .b, .mix: .largeTitle.bold().monospacedDigit()
        }
    }

    /// The secondary line under a row's title.
    var captionFont: Font {
        switch direction {
        case .a: .subheadline
        case .b, .mix: .caption
        }
    }

    var showsGlyphs: Bool { direction == .a }

    /// Whether summaries sit in a card (a list row) rather than on the background.
    var usesCards: Bool { direction != .b }

    /// Dense rows and compact section spacing.
    var isDense: Bool { direction != .a }

    /// Extra space above and below a row's content.
    var rowPadding: CGFloat { isDense ? 0 : 4 }
}

#if DEBUG
extension Theme {
    /// The theme `TALLY_THEME=A|B|mix`, and `TALLY_ACCENT=blue|indigo` pick; a missing or
    /// unknown value keeps the standard one.
    init(launchEnvironment env: [String: String]) {
        self.init(
            direction: env["TALLY_THEME"].flatMap { Direction(rawValue: $0.uppercased()) } ?? Self.standard.direction,
            accent: env["TALLY_ACCENT"].flatMap { Accent(rawValue: $0.lowercased()) } ?? Self.standard.accent
        )
    }
}
#endif

extension EnvironmentValues {
    @Entry var theme = Theme.standard
}

extension View {
    /// Applies the direction's list density: B and mix pack sections and rows closer.
    func themedList(_ theme: Theme) -> some View {
        listSectionSpacing(theme.isDense ? .compact : .default)
            .environment(\.defaultMinListRowHeight, theme.isDense ? 36 : 44)
    }
}
