import Foundation

/// A CSV's first records, decoded and read the way the server reads the file (api/app/services/import_parsers.py):
/// the header a mapping picks columns from, and the rows its preview reads.
struct CSVSample: Equatable {
    /// How many data rows are kept, after the header row.
    static let rowLimit = 20

    /// The first record as is (the server's header, even when blank), then up to `rowLimit` non-blank records,
    /// since the server skips blank ones.
    let records: [[String]]

    init(_ data: Data) {
        var reader = Reader(Self.text(of: data))
        var records: [[String]] = []
        while records.count <= Self.rowLimit, let record = reader.record() {
            if records.isEmpty || Self.hasContent(record) { records.append(record) }
        }
        self.records = records
    }

    /// The first row's fields, trimmed as the server trims them for matching. Empty when the file is.
    var names: [String] { (records.first ?? []).map(Self.trimmed) }

    /// With no header, every row is data: the 0-based indexes "0"…"n-1" of the widest of the first rows.
    var indexes: [String] {
        let width = records.filter(Self.hasContent).map(\.count).max() ?? 0
        return (0..<width).map(String.init)
    }

    /// The first row the server would read as a transaction: the first non-blank row after the header, if any.
    func firstDataRow(hasHeader: Bool) -> [String]? {
        records.dropFirst(hasHeader ? 1 : 0).first(where: Self.hasContent)
    }

    /// Where `column` is in a row: by header name, trimmed and case-folded as the server matches it (the last of
    /// two equal names wins, as in its lookup), or with no header, by index. Nil when the file has no such column.
    func position(of column: String, hasHeader: Bool) -> Int? {
        guard hasHeader else { return Int(column) }
        let wanted = Self.folded(column)
        return names.lastIndex { Self.folded($0) == wanted }
    }

    /// A cell as the server reads it: trimmed, and empty past the end of the row.
    static func cell(_ record: [String], at position: Int) -> String {
        position < record.count ? trimmed(record[position]) : ""
    }

    private static func trimmed(_ text: String) -> String { text.trimmingCharacters(in: .whitespacesAndNewlines) }

    private static func folded(_ name: String) -> String {
        trimmed(name).folding(options: .caseInsensitive, locale: nil)
    }

    private static func hasContent(_ record: [String]) -> Bool {
        record.contains { !trimmed($0).isEmpty }
    }

    /// Mirrors the server's `_decode`: UTF-8 with one leading BOM dropped when the whole file is valid UTF-8,
    /// otherwise Windows-1252 with undefined bytes replaced; then NUL characters removed.
    private static func text(of data: Data) -> String {
        var text = String(decoding: data, as: UTF8.self)
        if text.utf8.elementsEqual(data) {
            if text.unicodeScalars.first == "\u{FEFF}" { text.unicodeScalars.removeFirst() }
        } else {
            text = windows1252(data)
        }
        return text.replacingOccurrences(of: "\u{0}", with: "")
    }

    /// Python's `cp1252` decoding with `errors="replace"`: bytes 0x80–0x9F through the code page's table, the
    /// five it leaves undefined as U+FFFD, and every other byte as the Latin-1 character it names.
    private static func windows1252(_ data: Data) -> String {
        var scalars = String.UnicodeScalarView()
        for byte in data {
            let value = (0x80...0x9F).contains(byte) ? cp1252High[Int(byte) - 0x80] : UInt32(byte)
            scalars.append(Unicode.Scalar(value) ?? "\u{FFFD}")
        }
        return String(scalars)
    }

    private static let cp1252High: [UInt32] = [
        0x20AC, 0xFFFD, 0x201A, 0x0192, 0x201E, 0x2026, 0x2020, 0x2021,
        0x02C6, 0x2030, 0x0160, 0x2039, 0x0152, 0xFFFD, 0x017D, 0xFFFD,
        0xFFFD, 0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2013, 0x2014,
        0x02DC, 0x2122, 0x0161, 0x203A, 0x0153, 0xFFFD, 0x017E, 0x0178,
    ]

    /// Reads records as the `csv` module does: quoted fields with commas, doubled quotes and line breaks, and a
    /// record ended by CRLF, LF or CR. Scalars rather than characters, so CRLF (one `Character`) still ends it.
    private struct Reader {
        private var scalars: String.UnicodeScalarView.Iterator
        private var pending: Unicode.Scalar?

        init(_ text: String) {
            scalars = text.unicodeScalars.makeIterator()
            pending = scalars.next()
        }

        private mutating func next() -> Unicode.Scalar? {
            defer { pending = scalars.next() }
            return pending
        }

        /// The next record, or nil at the end of the text. A blank line is no fields.
        mutating func record() -> [String]? {
            guard pending != nil else { return nil }
            var fields: [String] = []
            var field = ""
            while let scalar = next() {
                switch scalar {
                case "\"" where field.isEmpty:
                    readQuoted(into: &field)
                case ",":
                    fields.append(field)
                    field = ""
                case "\r", "\n":
                    if scalar == "\r", pending == "\n" { _ = next() }
                    return Self.row(fields, field)
                default:
                    field.unicodeScalars.append(scalar)
                }
            }
            return Self.row(fields, field)
        }

        /// Reads a quoted field's text up to its closing quote, a doubled quote standing for one.
        private mutating func readQuoted(into field: inout String) {
            while let scalar = next() {
                if scalar != "\"" {
                    field.unicodeScalars.append(scalar)
                } else if pending == "\"" {
                    field.append("\"")
                    _ = next()
                } else {
                    return
                }
            }
        }

        /// The row so far ended by `last`; a blank line is no fields, as the `csv` module reads it.
        private static func row(_ fields: [String], _ last: String) -> [String] {
            fields.isEmpty && last.isEmpty ? [] : fields + [last]
        }
    }
}
