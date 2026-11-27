import SwiftUI

/// A monochrome SF Symbol for a category, in the secondary color and a fixed width so row text lines up.
struct CategoryGlyph: View {
    let symbol: String

    /// The symbol for the category named `name`: the first keyword that starts one of its words (or, for a short
    /// keyword, is one of its words or their plural). Nil means uncategorized.
    init(category name: String?) {
        symbol = Self.symbol(for: name)
    }

    init(systemImage symbol: String) {
        self.symbol = symbol
    }

    var body: some View {
        Image(systemName: symbol)
            .font(.body)
            .foregroundStyle(.secondary)
            .frame(width: 28)
            .accessibilityHidden(true)
    }

    static func symbol(for name: String?) -> String {
        guard let name else { return "questionmark.circle" }
        let words = name.lowercased().split { !$0.isLetter }
        return keywords.first { keyword, _ in
            words.contains { word in
                keyword.count < 4 ? word == keyword || word == keyword + "s" : word.hasPrefix(keyword)
            }
        }?.symbol ?? "tag"
    }

    private static let keywords: [(String, symbol: String)] = [
        ("uncategorized", "questionmark.circle"),
        ("grocer", "cart"),
        ("meal", "fork.knife"),
        ("dining", "fork.knife"),
        ("restaurant", "fork.knife"),
        ("coffee", "cup.and.saucer"),
        ("travel", "airplane"),
        ("fuel", "fuelpump"),
        ("gas", "fuelpump"),
        ("auto", "car"),
        ("car", "car"),
        ("salary", "banknote"),
        ("income", "banknote"),
        ("rent", "house"),
        ("housing", "house"),
        ("utilit", "bolt"),
        ("phone", "phone"),
        ("internet", "wifi"),
        ("health", "cross.case"),
        ("medical", "cross.case"),
        ("insurance", "shield"),
        ("entertainment", "tv"),
        ("streaming", "tv"),
        ("software", "app"),
        ("shopping", "bag"),
        ("office", "briefcase"),
        ("education", "book"),
        ("gift", "gift"),
    ]
}

/// A category's color for its spending bar. Known kinds of spending get a fixed color; any other category gets one
/// from the palette by its id, so it keeps the same color between launches. Over budget is shown in red instead.
enum CategoryColor {
    static let green = Color(red: 0.20, green: 0.78, blue: 0.48)
    static let blue = Color(red: 0.29, green: 0.62, blue: 0.96)
    static let orange = Color(red: 1.00, green: 0.62, blue: 0.26)
    static let yellow = Color(red: 0.97, green: 0.79, blue: 0.28)
    static let lavender = Color(red: 0.61, green: 0.55, blue: 0.95)
    static let teal = Color(red: 0.18, green: 0.77, blue: 0.71)
    static let coral = Color(red: 1.00, green: 0.48, blue: 0.48)

    static let palette: [Color] = [green, blue, orange, yellow, lavender, teal, coral]

    /// The color a category's bar or mark is drawn in, on Home and the Dashboard alike: red when over budget.
    static func barColor(for category: CategorySpend) -> Color {
        category.isOverBudget ? .red : color(for: category)
    }

    static func color(for category: CategorySpend) -> Color {
        guard let name = category.name else { return .gray }
        let words = name.lowercased().split { !$0.isLetter }
        if let match = keywords.first(where: { keyword, _ in words.contains { $0.hasPrefix(keyword) } }) {
            return match.color
        }
        return palette[Int(stableHash(category.id) % UInt64(palette.count))]
    }

    /// FNV-1a, since `hashValue` changes between launches.
    private static func stableHash(_ text: String) -> UInt64 {
        text.utf8.reduce(14_695_981_039_346_656_037) { ($0 ^ UInt64($1)) &* 1_099_511_628_211 }
    }

    private static let keywords: [(String, color: Color)] = [
        ("grocer", green),
        ("meal", orange),
        ("dining", orange),
        ("restaurant", orange),
        ("coffee", orange),
        ("travel", blue),
        ("fuel", yellow),
        ("gas", yellow),
        ("auto", yellow),
        ("car", yellow),
        ("rent", lavender),
        ("housing", lavender),
        ("utilit", teal),
        ("phone", teal),
        ("internet", teal),
        ("entertainment", coral),
        ("shopping", coral),
    ]
}
