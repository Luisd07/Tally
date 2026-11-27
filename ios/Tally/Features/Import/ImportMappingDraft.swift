import Foundation

/// The date formats a mapping offers, as `strptime` formats, plus one typed in.
enum DateFormatPreset: Hashable, CaseIterable {
    case monthDayYear, monthDayShortYear, yearMonthDay, dayMonthYear, custom

    var label: String {
        switch self {
        case .monthDayYear: "MM/DD/YYYY"
        case .monthDayShortYear: "MM/DD/YY"
        case .yearMonthDay: "YYYY-MM-DD"
        case .dayMonthYear: "DD/MM/YYYY"
        case .custom: "Custom"
        }
    }

    /// The `strptime` format; nil for `.custom`.
    var format: String? {
        switch self {
        case .monthDayYear: "%m/%d/%Y"
        case .monthDayShortYear: "%m/%d/%y"
        case .yearMonthDay: "%Y-%m-%d"
        case .dayMonthYear: "%d/%m/%Y"
        case .custom: nil
        }
    }

    /// The preset spelling `format`, or `.custom`.
    init(format: String) {
        self = Self.allCases.first { $0.format == format } ?? .custom
    }

    /// The name of the preset spelling `format`, or for a custom one, the format itself.
    static func label(for format: String) -> String {
        let preset = DateFormatPreset(format: format)
        return preset == .custom ? format : preset.label
    }
}

/// A CSV column mapping being edited. It holds both amount layouts' columns so switching between them loses
/// nothing, and `validated` mirrors the server's `ImportMappingIn` checks (api/app/schemas/imports.py).
struct ImportMappingDraft: Equatable {
    enum AmountMode: Hashable {
        case single, debitCredit
    }

    /// The server's longest column reference and its shortest and longest date format, in Unicode scalars as
    /// Python counts a string.
    static let maxColumnLength = 100
    static let dateFormatLengths = 2...40

    private(set) var hasHeader = true
    var dateColumn = ""
    var descriptionColumn = ""
    var amountMode = AmountMode.single
    var amountColumn = ""
    var debitColumn = ""
    var creditColumn = ""
    var datePreset = DateFormatPreset.monthDayYear
    var customDateFormat = ""
    var signConvention = SignConvention.negativeIsOut

    init() {}

    init(_ mapping: ImportMapping) {
        hasHeader = mapping.hasHeader
        dateColumn = mapping.dateColumn
        descriptionColumn = mapping.descriptionColumn
        switch mapping.amount {
        case .single(let column):
            amountColumn = column
        case .debitCredit(let debit, let credit):
            amountMode = .debitCredit
            debitColumn = debit
            creditColumn = credit
        }
        datePreset = DateFormatPreset(format: mapping.dateFormat)
        customDateFormat = datePreset == .custom ? mapping.dateFormat : ""
        signConvention = mapping.signConvention
    }

    var dateFormat: String { datePreset.format ?? customDateFormat }

    /// Sets whether the first row is a header. A change clears every column, since they switch between header
    /// names and indexes.
    mutating func setHasHeader(_ hasHeader: Bool) {
        guard hasHeader != self.hasHeader else { return }
        self.hasHeader = hasHeader
        dateColumn = ""
        descriptionColumn = ""
        amountColumn = ""
        debitColumn = ""
        creditColumn = ""
    }

    /// The mapping to save, or the first rule the draft breaks.
    var validated: Result<ImportMapping, Invalid> {
        guard !dateColumn.isEmpty else { return .failure(.missingDate) }
        guard !descriptionColumn.isEmpty else { return .failure(.missingDescription) }
        return resolvedAmount.flatMap { amount in
            if let broken = columnRule([dateColumn, descriptionColumn] + amount.columns) ?? dateFormatRule {
                return .failure(broken)
            }
            return .success(ImportMapping(
                dateColumn: dateColumn,
                descriptionColumn: descriptionColumn,
                amount: amount,
                dateFormat: dateFormat,
                signConvention: signConvention,
                hasHeader: hasHeader
            ))
        }
    }

    /// The amount layout's columns, once each it needs is chosen.
    private var resolvedAmount: Result<AmountColumns, Invalid> {
        switch amountMode {
        case .single:
            amountColumn.isEmpty ? .failure(.missingAmount) : .success(.single(amountColumn))
        case .debitCredit:
            debitColumn.isEmpty || creditColumn.isEmpty
                ? .failure(.missingDebitCredit)
                : .success(.debitCredit(debit: debitColumn, credit: creditColumn))
        }
    }

    /// The rule `columns` break: too long, or with no header, not indexes.
    private func columnRule(_ columns: [String]) -> Invalid? {
        if !columns.allSatisfy({ $0.unicodeScalars.count <= Self.maxColumnLength }) { return .columnTooLong }
        if !hasHeader, !columns.allSatisfy({ $0.allSatisfy { $0.isASCII && $0.isNumber } }) { return .notIndexes }
        return nil
    }

    /// The rule the date format breaks: its length, or a missing year, month or day.
    private var dateFormatRule: Invalid? {
        if !Self.dateFormatLengths.contains(dateFormat.unicodeScalars.count) { return .dateFormatLength }
        if !dateFormat.contains(/%[Yy]/) || !dateFormat.contains(/%[mbB]/) || !dateFormat.contains(/%d/) {
            return .dateFormatIncomplete
        }
        return nil
    }

    enum Invalid: FormRule, Equatable {
        case missingDate, missingDescription, missingAmount, missingDebitCredit, columnTooLong, notIndexes
        case dateFormatLength, dateFormatIncomplete

        var message: String {
            switch self {
            case .missingDate: "Choose the date column."
            case .missingDescription: "Choose the description column."
            case .missingAmount: "Choose the amount column."
            case .missingDebitCredit: "Choose both the debit and the credit column."
            case .columnTooLong: "A column name can't be over \(ImportMappingDraft.maxColumnLength) characters."
            case .notIndexes: "Without a header row, choose columns by position."
            case .dateFormatLength:
                "The date format must be \(ImportMappingDraft.dateFormatLengths.lowerBound) to "
                    + "\(ImportMappingDraft.dateFormatLengths.upperBound) characters."
            case .dateFormatIncomplete: "The date format must include the year, month and day, like %m/%d/%Y."
            }
        }
    }
}
