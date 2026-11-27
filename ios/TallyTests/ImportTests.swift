import Foundation
import os
import Testing
@testable import Tally

private let checkingMappingJSON = """
{"id":"0b5e1f7a-3c1d-4e55-9a51-1f2d7b9c8e01","account_id":"265cd02e-4a77-51ea-ad3a-4a9fbc282065",\
"date_column":"Date","amount_column":"Amount","debit_column":null,"credit_column":null,\
"description_column":"Description","date_format":"%m/%d/%Y","sign_convention":"negative_is_out","has_header":true}
"""

private let importResultJSON = """
{"inserted":3,"skipped_duplicate":0,"skipped_synced_range":0,"errors":[{"row":4,"message":"invalid date"}]}
"""

private final class ImportBundleToken {}

/// The CSV fixture `name` from `Fixtures/`.
private func csvFixture(_ name: String) throws -> Data {
    let url = try #require(Bundle(for: ImportBundleToken.self).url(forResource: name, withExtension: "csv"))
    return try Data(contentsOf: url)
}

/// `body` encoded as `APIClient` sends it, read back as a dictionary.
private func sentKeys(_ body: some Encodable) throws -> [String: Any] {
    let encoder = JSONEncoder()
    encoder.keyEncodingStrategy = .convertToSnakeCase
    return try #require(JSONSerialization.jsonObject(with: encoder.encode(body)) as? [String: Any])
}

/// Import values with no network: mappings, header parsing and validation, so they run in parallel.
@MainActor
struct ImportMappingTests {
    @Test func singleColumnMappingSendsOnlyTheAmountColumn() throws {
        let mapping = ImportMapping(
            dateColumn: "Date", descriptionColumn: "Description", amount: .single("Amount"), dateFormat: "%m/%d/%Y"
        )
        let keys = try sentKeys(mapping)
        #expect(keys["date_column"] as? String == "Date")
        #expect(keys["description_column"] as? String == "Description")
        #expect(keys["amount_column"] as? String == "Amount")
        #expect(keys["debit_column"] == nil)
        #expect(keys["credit_column"] == nil)
        #expect(keys["date_format"] as? String == "%m/%d/%Y")
        #expect(keys["sign_convention"] as? String == "negative_is_out")
        #expect(keys["has_header"] as? Bool == true)
    }

    @Test func debitCreditMappingSendsBothColumnsAndNoAmount() throws {
        let mapping = ImportMapping(
            dateColumn: "Posted Date",
            descriptionColumn: "Payee",
            amount: .debitCredit(debit: "Debit", credit: "Credit"),
            dateFormat: "%Y-%m-%d",
            signConvention: .positiveIsOut
        )
        let keys = try sentKeys(mapping)
        #expect(keys["amount_column"] == nil)
        #expect(keys["debit_column"] as? String == "Debit")
        #expect(keys["credit_column"] as? String == "Credit")
        #expect(keys["sign_convention"] as? String == "positive_is_out")
    }

    @Test func noHeaderMappingSendsIndexes() throws {
        let mapping = ImportMapping(
            dateColumn: "0", descriptionColumn: "1", amount: .single("2"), dateFormat: "%d/%m/%Y", hasHeader: false
        )
        let keys = try sentKeys(mapping)
        #expect(keys["has_header"] as? Bool == false)
        #expect(keys["date_column"] as? String == "0")
        #expect(keys["amount_column"] as? String == "2")
    }

    @Test func mappingDecodesEitherAmountLayout() throws {
        let single = try JSONDecoder.api.decode(ImportMapping.self, from: Data(checkingMappingJSON.utf8))
        #expect(single.amount == .single("Amount"))
        #expect(single.dateFormat == "%m/%d/%Y")
        #expect(single.hasHeader)
        let pairJSON = """
        {"id":"0b5e1f7a-3c1d-4e55-9a51-1f2d7b9c8e01","account_id":"265cd02e-4a77-51ea-ad3a-4a9fbc282065",\
        "date_column":"0","amount_column":null,"debit_column":"2","credit_column":"3","description_column":"1",\
        "date_format":"%Y-%m-%d","sign_convention":"sideways","has_header":false}
        """
        let pair = try JSONDecoder.api.decode(ImportMapping.self, from: Data(pairJSON.utf8))
        #expect(pair.amount == .debitCredit(debit: "2", credit: "3"))
        #expect(pair.signConvention == .other("sideways"))
        #expect(!pair.hasHeader)
    }

    @Test func resultDecodesWithItsSummaryAndRowErrors() throws {
        let result = try JSONDecoder.api.decode(ImportResult.self, from: Data(importResultJSON.utf8))
        #expect(result.summary == "3 added · 0 duplicates · 0 already synced")
        #expect(result.errors == [ImportRowError(row: 4, message: "invalid date")])
    }

    @Test func headerIsTheFirstRowOfEitherSample() throws {
        #expect(CSVSample(try csvFixture("live-import-checking")).names == ["Date", "Description", "Amount"])
        #expect(CSVSample(try csvFixture("bank-export-card")).names == ["Posted Date", "Payee", "Debit", "Credit"])
    }

    @Test func headerWithoutAHeaderRowIsTheFirstRowsIndexes() throws {
        #expect(CSVSample(try csvFixture("live-import-checking")).indexes == ["0", "1", "2"])
        #expect(CSVSample(Data()).indexes == [])
    }

    @Test func headerStripsABOMAndReadsQuotedFields() {
        let csv = Data([0xEF, 0xBB, 0xBF]) + Data("\"Date\",\"Payee, Name\",\"Say \"\"hi\"\"\", Amount \r\n1,2,3,4\r\n".utf8)
        #expect(CSVSample(csv).names == ["Date", "Payee, Name", "Say \"hi\"", "Amount"])
    }

    @Test func headerEndsAtLFOrCRLFButNotInsideQuotes() {
        #expect(CSVSample(Data("A,B\nc,d\n".utf8)).names == ["A", "B"])
        #expect(CSVSample(Data("A,B\r\nc,d".utf8)).names == ["A", "B"])
        #expect(CSVSample(Data("\"Line\nBreak\",B\nc,d".utf8)).names == ["Line\nBreak", "B"])
        #expect(CSVSample(Data("A,,B".utf8)).names == ["A", "", "B"])
    }

    @Test func headerReadsWindows1252WhenNotUTF8() {
        #expect(CSVSample(Data([0x43, 0x61, 0x66, 0xE9, 0x2C, 0x42])).names == ["Café", "B"])
    }

    /// `_decode` strips the BOM only through `utf-8-sig`, so a file that isn't valid UTF-8 keeps it, read as
    /// Windows-1252; the code page's undefined bytes become U+FFFD.
    @Test func headerKeepsTheBOMWhenTheFileIsNotUTF8() {
        let csv = Data([0xEF, 0xBB, 0xBF]) + Data("Date,B\n".utf8) + Data([0x81])
        #expect(CSVSample(csv).names == ["ï»¿Date", "B"])
        #expect(CSVSample(Data([0x41, 0x81, 0x2C, 0x42])).names == ["A\u{FFFD}", "B"])
    }

    @Test func headerDropsNULCharactersAsTheServerDoes() {
        let utf16 = Data([0x44, 0x00, 0x61, 0x00, 0x2C, 0x00, 0x42, 0x00, 0x0A, 0x00])
        #expect(CSVSample(utf16).names == ["Da", "B"])
    }

    /// Without a header the server reads every row and skips blank ones, so the indexes cover the widest row.
    @Test func indexesSkipABlankFirstLineAndCoverTheWidestRow() {
        #expect(CSVSample(Data("\nA,B\nc,d,e\n".utf8)).indexes == ["0", "1", "2"])
        #expect(CSVSample(Data("A\n\n\nb,c\n".utf8)).firstDataRow(hasHeader: false) == ["A"])
    }

    /// With a header the server takes the first line as it is, blank or not, and reads data from the next.
    @Test func aBlankFirstLineIsAnEmptyHeader() {
        let sample = CSVSample(Data("\nA,B\n\nc,d\n".utf8))
        #expect(sample.names == [])
        #expect(sample.firstDataRow(hasHeader: true) == ["A", "B"])
    }

    @Test func positionMatchesNamesTrimmedAndCaseFolded() {
        let sample = CSVSample(Data(" Posted Date ,STRASSE,Amount,amount\n".utf8))
        #expect(sample.position(of: "posted date", hasHeader: true) == 0)
        #expect(sample.position(of: "straße", hasHeader: true) == 1)
        #expect(sample.position(of: "Amount", hasHeader: true) == 3)
        #expect(sample.position(of: "Memo", hasHeader: true) == nil)
        #expect(sample.position(of: "2", hasHeader: false) == 2)
    }

    private func checkingDraft() -> ImportMappingDraft {
        var draft = ImportMappingDraft()
        draft.dateColumn = "Date"
        draft.descriptionColumn = "Description"
        draft.amountColumn = "Amount"
        return draft
    }

    @Test func validDraftBuildsTheMappingWithThePresetFormat() {
        var draft = checkingDraft()
        #expect(draft.validated == .success(ImportMapping(
            dateColumn: "Date", descriptionColumn: "Description", amount: .single("Amount"), dateFormat: "%m/%d/%Y"
        )))
        draft.datePreset = .yearMonthDay
        #expect(draft.dateFormat == "%Y-%m-%d")
        draft.datePreset = .dayMonthYear
        #expect(draft.dateFormat == "%d/%m/%Y")
        draft.datePreset = .monthDayShortYear
        #expect(draft.dateFormat == "%m/%d/%y")
        draft.datePreset = .custom
        draft.customDateFormat = "%d.%m.%y"
        #expect((try? draft.validated.get())?.dateFormat == "%d.%m.%y")
    }

    @Test func draftNeedsEveryColumn() {
        var draft = checkingDraft()
        draft.dateColumn = ""
        #expect(draft.validated == .failure(.missingDate))
        draft = checkingDraft()
        draft.descriptionColumn = ""
        #expect(draft.validated == .failure(.missingDescription))
        draft = checkingDraft()
        draft.amountColumn = ""
        #expect(draft.validated == .failure(.missingAmount))
    }

    @Test func debitCreditDraftNeedsBothAndSendsNoAmount() {
        var draft = checkingDraft()
        draft.amountMode = .debitCredit
        draft.debitColumn = "Debit"
        #expect(draft.validated == .failure(.missingDebitCredit))
        draft.creditColumn = "Credit"
        #expect((try? draft.validated.get())?.amount == .debitCredit(debit: "Debit", credit: "Credit"))
    }

    @Test func draftWithoutAHeaderNeedsIndexes() {
        var draft = checkingDraft()
        draft.setHasHeader(false)
        #expect(draft.dateColumn.isEmpty && draft.descriptionColumn.isEmpty && draft.amountColumn.isEmpty)
        draft.dateColumn = "0"
        draft.descriptionColumn = "Description"
        draft.amountColumn = "2"
        #expect(draft.validated == .failure(.notIndexes))
        draft.descriptionColumn = "1"
        #expect((try? draft.validated.get())?.hasHeader == false)
    }

    @Test func draftDateFormatNeedsYearMonthAndDay() {
        var draft = checkingDraft()
        draft.datePreset = .custom
        draft.customDateFormat = "%m/%d"
        #expect(draft.validated == .failure(.dateFormatIncomplete))
        draft.customDateFormat = "%"
        #expect(draft.validated == .failure(.dateFormatLength))
        draft.customDateFormat = "%d %b %Y"
        #expect((try? draft.validated.get())?.dateFormat == "%d %b %Y")
    }

    @Test func draftFromASavedMappingKeepsItsPresetOrCustomFormat() {
        let preset = ImportMapping(
            dateColumn: "Date", descriptionColumn: "Description", amount: .single("Amount"), dateFormat: "%Y-%m-%d"
        )
        #expect(ImportMappingDraft(preset).datePreset == .yearMonthDay)
        #expect(ImportMappingDraft(preset).validated == .success(preset))
        let custom = ImportMapping(
            dateColumn: "0",
            descriptionColumn: "1",
            amount: .debitCredit(debit: "2", credit: "3"),
            dateFormat: "%d.%m.%Y",
            signConvention: .positiveIsOut,
            hasHeader: false
        )
        let draft = ImportMappingDraft(custom)
        #expect(draft.datePreset == .custom)
        #expect(draft.customDateFormat == "%d.%m.%Y")
        #expect(draft.validated == .success(custom))
    }

    @Test func presetLabelNamesAPresetOrShowsACustomFormat() {
        #expect(DateFormatPreset.label(for: "%Y-%m-%d") == "YYYY-MM-DD")
        #expect(DateFormatPreset.label(for: "%m/%d/%y") == "MM/DD/YY")
        #expect(DateFormatPreset.label(for: "%d.%m.%Y") == "%d.%m.%Y")
        #expect(DateFormatPreset(format: "%m/%d/%y") != .custom)
    }

    @Test func fileKindFollowsTheExtension() {
        #expect(ImportFile(name: "statement.OFX", data: Data()).kind == .ofx)
        #expect(ImportFile(name: "x.QFX", data: Data()).kind == .ofx)
        #expect(ImportFile(name: "statement.csv", data: Data()).kind == .csv)
        #expect(ImportFile(name: "statement.ofx.csv", data: Data()).kind == .csv)
    }

    /// Writes `count` bytes to a temp file and reads it back the way the importer does.
    private func readTempFile(of count: Int) throws -> ImportFile {
        let url = FileManager.default.temporaryDirectory.appending(path: "import-\(UUID().uuidString).csv")
        try Data(count: count).write(to: url)
        defer { try? FileManager.default.removeItem(at: url) }
        return try ImportFile.read(from: url)
    }

    @Test func readTakesAFileAtTheLimitAndStopsOneBytePast() throws {
        let atLimit = try readTempFile(of: ImportFile.maxBytes)
        #expect(atLimit.data.count == 2_000_000)
        #expect(!atLimit.isTooLarge)
        let overLimit = try readTempFile(of: ImportFile.maxBytes + 1)
        #expect(overLimit.data.count == 2_000_001)
        #expect(overLimit.isTooLarge)
        #expect(try readTempFile(of: 3_000_000).data.count == 2_000_001)
    }

    @Test func pickFileReadsThePickedFileOrSaysWhyItCouldNot() throws {
        let model = ImportModel()
        model.pickFile(.failure(CocoaError(.fileReadNoPermission)))
        #expect(model.importFailure?.hasPrefix("Couldn't read the file: ") == true)
        #expect(model.file == nil)
        let url = FileManager.default.temporaryDirectory.appending(path: "live-import-checking.csv")
        try csvFixture("live-import-checking").write(to: url)
        defer { try? FileManager.default.removeItem(at: url) }
        model.pickFile(.success(url))
        #expect(model.file?.name == "live-import-checking.csv")
        #expect(model.importFailure == nil)
        #expect(model.columns == ["Date", "Description", "Amount"])
    }

    @Test func columnOptionsAddASavedChoiceTheFileLacks() throws {
        let model = ImportModel()
        model.pick(ImportFile(name: "card.csv", data: try csvFixture("bank-export-card")))
        #expect(model.columnOptions(including: "") == ["Posted Date", "Payee", "Debit", "Credit"])
        #expect(model.columnOptions(including: "Payee") == ["Posted Date", "Payee", "Debit", "Credit"])
        #expect(model.columnOptions(including: "Memo") == ["Posted Date", "Payee", "Debit", "Credit", "Memo"])
    }
}

/// The first-row preview: how a draft reads a file's first data row, with no network.
@MainActor
struct ImportPreviewTests {
    private func makeDraft(format: String, sign: SignConvention = .negativeIsOut) -> ImportMappingDraft {
        var draft = ImportMappingDraft()
        draft.dateColumn = "Date"
        draft.descriptionColumn = "Description"
        draft.amountColumn = "Amount"
        draft.datePreset = DateFormatPreset(format: format)
        draft.customDateFormat = draft.datePreset == .custom ? format : ""
        draft.signConvention = sign
        return draft
    }

    private func read(_ csv: String, _ draft: ImportMappingDraft) throws -> ImportPreview {
        try #require(ImportPreview(sample: CSVSample(Data(csv.utf8)), draft: draft))
    }

    private func problem(_ message: String) -> ImportPreview.Problem { ImportPreview.Problem(message) }

    @Test func readsTheFirstRowWithMonthDayYear() throws {
        let csv = String(decoding: try csvFixture("live-import-checking"), as: UTF8.self)
        let preview = try read(csv, makeDraft(format: "%m/%d/%Y"))
        #expect(preview.date == .success(CalendarDate(year: 2025, month: 1, day: 5)))
        #expect(preview.amount == .success(Money(Decimal(string: "-4.75")!)))
        #expect(preview.description == .success("LIVE IMPORT COFFEE"))
        let cost = Money(Decimal(string: "4.75")!)
        #expect(ImportPreview.amountText(cost.negated) == "−\(cost.formatted()) (money out)")
        #expect(ImportPreview.amountText(cost) == "\(cost.formatted()) (money in)")
    }

    @Test func readsISOAndDayMonthDates() throws {
        let iso = try read("Date,Description,Amount\n2025-02-03,X,1\n", makeDraft(format: "%Y-%m-%d"))
        #expect(iso.date == .success(CalendarDate(year: 2025, month: 2, day: 3)))
        let dayMonth = try read("Date,Description,Amount\n03/02/2025,X,1\n", makeDraft(format: "%d/%m/%Y"))
        #expect(dayMonth.date == .success(CalendarDate(year: 2025, month: 2, day: 3)))
        let wrongOrder = try read("Date,Description,Amount\n13/02/2025,X,1\n", makeDraft(format: "%m/%d/%Y"))
        #expect(wrongOrder.date == .failure(problem("Can't read “13/02/2025” as MM/DD/YYYY.")))
    }

    /// Python's widths: %Y needs four digits and %y two, so a two-digit year fails the four-digit preset.
    @Test func yearWidthsFollowStrptime() throws {
        let short = "Date,Description,Amount\n01/05/25,X,1\n"
        #expect(try read(short, makeDraft(format: "%m/%d/%Y")).date
            == .failure(problem("Can't read “01/05/25” as MM/DD/YYYY.")))
        #expect(try read(short, makeDraft(format: "%m/%d/%y")).date == .success(CalendarDate(year: 2025, month: 1, day: 5)))
        let old = "Date,Description,Amount\n1/5/70,X,1\n"
        #expect(try read(old, makeDraft(format: "%m/%d/%y")).date == .success(CalendarDate(year: 1970, month: 1, day: 5)))
    }

    @Test func readsMonthNamesAndLiterals() throws {
        let csv = "Date,Description,Amount\n3 feb 2025,X,1\n"
        #expect(try read(csv, makeDraft(format: "%d %b %Y")).date == .success(CalendarDate(year: 2025, month: 2, day: 3)))
        let full = "Date,Description,Amount\n3 February 2025,X,1\n"
        #expect(try read(full, makeDraft(format: "%d %B %Y")).date == .success(CalendarDate(year: 2025, month: 2, day: 3)))
        #expect(try read(full, makeDraft(format: "%d %b %Y")).date.isFailure)
        let dotted = "Date,Description,Amount\n2025.1.2,X,1\n"
        #expect(try read(dotted, makeDraft(format: "%Y.%m.%d")).date == .success(CalendarDate(year: 2025, month: 1, day: 2)))
    }

    @Test func anUnknownDirectiveHasNoDatePreview() throws {
        let csv = "Date,Description,Amount\n2025-02-03 10:00,X,1\n"
        let preview = try read(csv, makeDraft(format: "%Y-%m-%d %H:%M"))
        #expect(preview.date == .failure(problem("Preview unavailable for this format.")))
        #expect(preview.amount == .success(Money(1)))
    }

    @Test func positiveIsOutFlipsTheSign() throws {
        let csv = "Date,Description,Amount\n01/05/2025,COFFEE,4.75\n"
        #expect(try read(csv, makeDraft(format: "%m/%d/%Y")).amount == .success(Money(Decimal(string: "4.75")!)))
        let flipped = try read(csv, makeDraft(format: "%m/%d/%Y", sign: .positiveIsOut))
        #expect(flipped.amount == .success(Money(Decimal(string: "-4.75")!)))
    }

    @Test func debitAndCreditReadAsMoneyOutAndIn() throws {
        var draft = makeDraft(format: "%Y-%m-%d")
        draft.dateColumn = "Posted Date"
        draft.descriptionColumn = "Payee"
        draft.amountMode = .debitCredit
        draft.debitColumn = "Debit"
        draft.creditColumn = "Credit"
        let sample = CSVSample(try csvFixture("bank-export-card"))
        #expect(sample.firstDataRow(hasHeader: true) == ["2025-02-03", "FAKE GROCER", "42.10", ""])
        let preview = try #require(ImportPreview(sample: sample, draft: draft))
        #expect(preview.amount == .success(Money(Decimal(string: "-42.10")!)))
        #expect(preview.date == .success(CalendarDate(year: 2025, month: 2, day: 3)))
        let credit = try read("Posted Date,Payee,Debit,Credit\n2025-02-07,PAY,,100.00\n", draft)
        #expect(credit.amount == .success(Money(100)))
        let neither = try read("Posted Date,Payee,Debit,Credit\n2025-02-07,PAY,,\n", draft)
        #expect(neither.amount == .failure(problem("No debit or credit amount.")))
    }

    @Test func amountsReadAsTheServerReadsThem() throws {
        let draft = makeDraft(format: "%m/%d/%Y")
        func amount(_ cell: String) throws -> Result<Money, ImportPreview.Problem> {
            try read("Date,Description,Amount\n01/05/2025,X,\"\(cell)\"\n", draft).amount
        }
        #expect(try amount("$1,234.50") == .success(Money(Decimal(string: "1234.50")!)))
        #expect(try amount("(12.00)") == .success(Money(-12)))
        #expect(try amount("12.00-") == .success(Money(-12)))
        #expect(try amount("abc") == .failure(problem("Can't read “abc” as an amount.")))
        #expect(try amount("12abc") == .failure(problem("Can't read “12abc” as an amount.")))
        #expect(try amount("1.234") == .failure(problem("“1.234” has more than 2 decimal places.")))
        #expect(try amount("") == .failure(problem("No amount.")))
    }

    @Test func missingColumnsAndDescriptionsSayWhy() throws {
        var draft = makeDraft(format: "%m/%d/%Y")
        draft.amountColumn = "Total"
        draft.descriptionColumn = ""
        let preview = try read("Date,Description,Amount\n01/05/2025, ,1\n", draft)
        #expect(preview.amount == .failure(problem("This file has no column “Total”.")))
        #expect(preview.description == .failure(problem("Choose a column.")))
        draft.descriptionColumn = "description"
        #expect(try read("Date,Description,Amount\n01/05/2025, ,1\n", draft).description
            == .failure(problem("No description.")))
    }

    @Test func noDataRowHasNoPreview() {
        let headerOnly = CSVSample(Data("Date,Description,Amount\n\n".utf8))
        #expect(ImportPreview(sample: headerOnly, draft: makeDraft(format: "%m/%d/%Y")) == nil)
    }
}

private extension Result {
    var isFailure: Bool {
        if case .failure = self { true } else { false }
    }
}

extension APIClientTests {
    private var importAccountId: UUID { UUID(uuidString: "265CD02E-4A77-51EA-AD3A-4A9FBC282065")! }

    /// Serves the mapping GET with `mapping` (404 when nil), echoes a PUT body back, and answers an upload with
    /// `upload`, streaming every request seen.
    private func serveImport(
        mapping: String?, upload: (status: Int, body: String) = (200, importResultJSON)
    ) -> AsyncStream<URLRequest> {
        let (requests, seen) = AsyncStream.makeStream(of: URLRequest.self)
        let notFound = #"{"error":{"code":"not_found","message":"Not found.","request_id":"r404"}}"#
        StubURLProtocol.respond { request in
            seen.yield(request)
            let (status, body): (Int, Data) = switch request.httpMethod {
            case "GET": mapping.map { (200, Data($0.utf8)) } ?? (404, Data(notFound.utf8))
            case "PUT": (200, await self.bodyData(request) ?? Data())
            default: (upload.status, Data(upload.body.utf8))
            }
            return (StubURLProtocol.response(to: request, status: status), body)
        }
        return requests
    }

    @Test func importMappingIsNilWhenTheAccountHasNone() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        var requests = serveImport(mapping: nil).makeAsyncIterator()

        let mapping = try await api.importMapping(accountId: importAccountId)
        let request = try #require(await requests.next())
        #expect(mapping == nil)
        #expect(request.httpMethod == "GET")
        #expect(request.url?.path() == "/api/imports/mappings/265cd02e-4a77-51ea-ad3a-4a9fbc282065")
    }

    @Test func saveImportMappingPutsTheMapping() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        var requests = serveImport(mapping: nil).makeAsyncIterator()
        let mapping = ImportMapping(
            dateColumn: "Posted Date",
            descriptionColumn: "Payee",
            amount: .debitCredit(debit: "Debit", credit: "Credit"),
            dateFormat: "%Y-%m-%d"
        )

        let saved = try await api.saveImportMapping(accountId: importAccountId, mapping)
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "PUT")
        #expect(request.url?.path() == "/api/imports/mappings/265cd02e-4a77-51ea-ad3a-4a9fbc282065")
        let sent = try #require(bodyData(request))
        let body = try #require(JSONSerialization.jsonObject(with: sent) as? [String: Any])
        #expect(body["debit_column"] as? String == "Debit")
        #expect(body["amount_column"] == nil)
        #expect(saved == mapping)
    }

    /// The upload carries the account as a query item and the file's exact bytes in the `file` field.
    @Test func importCSVSendsTheFileWithTheAccountQuery() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        var requests = serveImport(mapping: checkingMappingJSON).makeAsyncIterator()
        let csv = try bundleFixture("live-import-checking", ext: "csv")

        let result = try await api.importCSV(accountId: importAccountId, data: csv, filename: "live-import-checking.csv")
        let request = try #require(await requests.next())
        #expect(request.httpMethod == "POST")
        let url = try #require(request.url)
        #expect(url.path() == "/api/imports/csv")
        let query = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems
        #expect(query == [URLQueryItem(name: "account_id", value: "265cd02e-4a77-51ea-ad3a-4a9fbc282065")])
        let contentType = try #require(request.value(forHTTPHeaderField: "Content-Type"))
        #expect(contentType.hasPrefix("multipart/form-data; boundary="))
        let body = try #require(bodyData(request))
        let text = String(decoding: body, as: UTF8.self)
        #expect(text.contains(
            "name=\"file\"; filename=\"live-import-checking.csv\"\r\nContent-Type: text/csv\r\n\r\n"
        ))
        #expect(body.range(of: Data("\r\n\r\n".utf8) + csv + Data("\r\n--".utf8)) != nil)
        #expect(result.inserted == 3)
        #expect(result.errors.count == 1)
    }

    @Test func importOFXPostsToTheOFXEndpoint() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        var requests = serveImport(mapping: nil).makeAsyncIterator()

        _ = try await api.importOFX(accountId: importAccountId, data: Data("<OFX>".utf8), filename: "s.ofx")
        let request = try #require(await requests.next())
        let url = try #require(request.url)
        #expect(url.path() == "/api/imports/ofx")
        #expect(url.query() == "account_id=265cd02e-4a77-51ea-ad3a-4a9fbc282065")
        let text = String(decoding: try #require(bodyData(request)), as: UTF8.self)
        #expect(text.contains("name=\"file\"; filename=\"s.ofx\"\r\nContent-Type: application/x-ofx\r\n\r\n<OFX>\r\n"))
    }

    @Test func importModelUploadsWithTheSavedMappingAndShowsTheResult() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        _ = serveImport(mapping: checkingMappingJSON)
        let model = ImportModel()

        await model.select(accountId: importAccountId, api: api)
        #expect(model.savedMapping?.amount == .single("Amount"))
        #expect(!model.isEditingMapping)
        model.pick(ImportFile(name: "live-import-checking.csv", data: try bundleFixture("live-import-checking", ext: "csv")))
        #expect(model.columns == ["Date", "Description", "Amount"])
        #expect(model.canImport)
        await model.importFile(api: api)
        #expect(model.result?.summary == "3 added · 0 duplicates · 0 already synced")
        #expect(model.importFailure == nil)
        #expect(model.resultFileName == "live-import-checking.csv")
        #expect(model.file == nil)
        #expect(model.columns.isEmpty)
        #expect(!model.canImport)
    }

    @Test func importModelEditsAndSavesAMappingWhenThereIsNone() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        _ = serveImport(mapping: nil)
        let model = ImportModel()

        await model.select(accountId: importAccountId, api: api)
        model.pick(ImportFile(name: "bank-export-card.csv", data: try bundleFixture("bank-export-card", ext: "csv")))
        #expect(model.isEditingMapping)
        #expect(!model.canImport)
        model.draft.dateColumn = "Posted Date"
        model.draft.descriptionColumn = "Payee"
        model.draft.amountMode = .debitCredit
        model.draft.debitColumn = "Debit"
        model.draft.creditColumn = "Credit"
        model.draft.datePreset = .yearMonthDay

        #expect(await model.saveMapping(api: api))
        #expect(model.savedMapping?.amount == .debitCredit(debit: "Debit", credit: "Credit"))
        #expect(!model.isEditingMapping)
        #expect(model.canImport)
    }

    @Test func importModelShowsTheServersRejection() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let envelope = #"{"error":{"code":"validation_error","message":"Invalid request.","request_id":"r422","details":[{"type":"value_error","loc":["body"],"msg":"Value error, save a column mapping for this account first"}]}}"#
        _ = serveImport(mapping: checkingMappingJSON, upload: (422, envelope))
        let model = ImportModel()

        await model.select(accountId: importAccountId, api: api)
        model.pick(ImportFile(name: "live-import-checking.csv", data: try bundleFixture("live-import-checking", ext: "csv")))
        await model.importFile(api: api)
        #expect(model.importFailure == "save a column mapping for this account first")
        #expect(model.result == nil)
        #expect(model.file != nil)
    }

    @Test func importModelRefusesAnOversizeFileWithoutARequest() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let uploads = OSAllocatedUnfairLock(initialState: 0)
        StubURLProtocol.respond { request in
            if request.httpMethod == "POST" { uploads.withLock { $0 += 1 } }
            return (StubURLProtocol.response(to: request, status: 404), Data())
        }
        let model = ImportModel()

        await model.select(accountId: importAccountId, api: api)
        model.pick(ImportFile(name: "big.ofx", data: Data(count: ImportFile.maxBytes + 1)))
        #expect(model.isFileTooLarge)
        #expect(model.canImport)
        await model.importFile(api: api)
        #expect(model.importFailure?.contains("over 2 MB") == true)
        #expect(model.result == nil)
        #expect(uploads.withLock { $0 } == 0)
    }

    @Test func importModelKeepsTheTooLargeMessageAcrossAnAccountChange() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        _ = serveImport(mapping: nil)
        let model = ImportModel()

        await model.select(accountId: importAccountId, api: api)
        model.pick(ImportFile(name: "big.csv", data: Data(count: ImportFile.maxBytes + 1)))
        await model.select(accountId: UUID(), api: api)
        #expect(model.importFailure?.contains("over 2 MB") == true)
        await model.select(accountId: nil, api: api)
        #expect(model.importFailure?.contains("over 2 MB") == true)
    }

    /// The server matches header names trimmed and case-folded, and refuses the whole file for one it can't find.
    @Test func importModelWarnsOfSavedColumnsTheFileLacks() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        _ = serveImport(mapping: checkingMappingJSON)
        let model = ImportModel()

        await model.select(accountId: importAccountId, api: api)
        model.pick(ImportFile(name: "card.csv", data: try bundleFixture("bank-export-card", ext: "csv")))
        #expect(model.missingColumns == ["Date", "Description", "Amount"])
        #expect(!model.canImport)
        model.pick(ImportFile(name: "lower.csv", data: Data(" date ,DESCRIPTION,amount\n01/05/2025,X,1\n".utf8)))
        #expect(model.missingColumns == [])
        #expect(model.canImport)
        model.pick(ImportFile(name: "s.ofx", data: Data("<OFX>".utf8)))
        #expect(model.missingColumns == [])
    }

    /// A mapping load still in flight when the account changes is dropped; the later account's mapping wins.
    @Test func importModelDropsALateMappingForAPreviousAccount() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let first = UUID(uuidString: "11111111-1111-4111-8111-111111111111")!
        let pairJSON = checkingMappingJSON
            .replacingOccurrences(of: #""amount_column":"Amount","debit_column":null,"credit_column":null"#,
                                  with: #""amount_column":null,"debit_column":"Debit","credit_column":"Credit""#)
        let (seen, saw) = AsyncStream.makeStream(of: String.self)
        let (gate, open) = AsyncStream.makeStream(of: Void.self)
        StubURLProtocol.respond { request in
            let path = request.url?.path() ?? ""
            saw.yield(path)
            if path.hasSuffix(first.uuidString.lowercased()) {
                for await _ in gate { break }
                return (StubURLProtocol.response(to: request, status: 200), Data(pairJSON.utf8))
            }
            return (StubURLProtocol.response(to: request, status: 200), Data(checkingMappingJSON.utf8))
        }
        let model = ImportModel()

        let late = Task { await model.select(accountId: first, api: api) }
        var paths = seen.makeAsyncIterator()
        _ = await paths.next()
        await model.select(accountId: importAccountId, api: api)
        open.yield()
        await late.value
        #expect(model.accountId == importAccountId)
        #expect(model.savedMapping?.amount == .single("Amount"))
        #expect(model.draft.amountColumn == "Amount")
    }

    @Test func importModelCancelsEditsBackToTheSavedMapping() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        _ = serveImport(mapping: checkingMappingJSON)
        let model = ImportModel()

        await model.select(accountId: importAccountId, api: api)
        let saved = try #require(model.savedMapping)
        model.editMapping()
        model.draft.amountColumn = "Total"
        model.draft.datePreset = .dayMonthYear
        #expect(!model.canImport)
        model.cancelEditingMapping()
        #expect(!model.isEditingMapping)
        #expect(model.draft == ImportMappingDraft(saved))
    }

    @Test func importModelReloadsAMappingThatFailedToLoad() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        _ = reply(500)
        let model = ImportModel()

        await model.select(accountId: importAccountId, api: api)
        guard case .failed = model.mappingLoad else {
            Issue.record("expected a failed load, got \(model.mappingLoad)")
            return
        }
        _ = serveImport(mapping: checkingMappingJSON)
        await model.reloadMapping(api: api)
        #expect(model.savedMapping?.amount == .single("Amount"))
        #expect(!model.isEditingMapping)
    }
}

// MARK: - AI column guess (#172)

extension APIClientTests {
    @Test func guessingTheColumnsFillsTheDraftForReview() async throws {
        defer { try? fixture.cleanUp() }
        let api = makeClient(fixture.make())
        let (requests, seen) = AsyncStream.makeStream(of: URLRequest.self)
        let notFound = #"{"error":{"code":"not_found","message":"Not found.","request_id":"r404"}}"#
        let guess = #"{"date_column":"Date","amount_column":"Amount","debit_column":null,"credit_column":null,"description_column":"Description","date_format":"%m/%d/%Y","sign_convention":"negative_is_out","has_header":true}"#
        StubURLProtocol.respond { request in
            seen.yield(request)
            if request.url?.path() == "/api/ai/guess-mapping" {
                return (StubURLProtocol.response(to: request, status: 200), Data(guess.utf8))
            }
            return (StubURLProtocol.response(to: request, status: 404), Data(notFound.utf8))
        }
        let model = ImportModel()
        await model.select(accountId: UUID(), api: api)
        let csv = (["Date,Description,Amount"] + (1...9).map { "10/0\($0)/2026,ROW \($0),-1.00" }).joined(separator: "\n")
        model.pick(ImportFile(name: "new.csv", data: Data(csv.utf8)))

        await model.guessMapping(api: api)

        #expect(model.isEditingMapping)
        #expect(model.guessError == nil)
        #expect(model.draft.dateColumn == "Date")
        #expect(model.draft.amountColumn == "Amount")
        #expect(model.draft.dateFormat == "%m/%d/%Y")
        var iterator = requests.makeAsyncIterator()
        _ = await iterator.next() // the mapping GET
        let request = try #require(await iterator.next())
        let body = try #require(bodyData(request))
        let json = try #require(JSONSerialization.jsonObject(with: body) as? [String: [[String]]])
        #expect(json["records"]?.count == ImportModel.guessRecords)
        #expect(json["records"]?.first == ["Date", "Description", "Amount"])
    }
}
