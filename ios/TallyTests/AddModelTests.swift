import Foundation
import Testing
import UIKit
@testable import Tally

@MainActor
struct AddModelTests {
    let suiteName = "test.add.\(UUID().uuidString)"
    var defaults: UserDefaults { UserDefaults(suiteName: suiteName)! }
    var preferences: CapturePreferences { CapturePreferences(defaults: defaults) }

    func account(id: UUID = UUID(), name: String = "Chase", active: Bool = true) throws -> Account {
        let json = """
        {"id":"\(id.uuidString.lowercased())","name":"\(name)","kind":"credit_card","institution":null,
         "simplefin_account_id":null,"last4":"1234","due_day":null,"planned_payment":null,"apr":null,
         "promo_apr_end":null,"active":\(active),"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}
        """
        return try decoder.decode(Account.self, from: Data(json.utf8))
    }

    func category(id: UUID = UUID(), name: String, kind: String = "expense") throws -> Tally.Category {
        let json = """
        {"id":"\(id.uuidString.lowercased())","name":"\(name)","kind":"\(kind)","tax_line":null,"deductible_pct":0,
         "monthly_budget":null,"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}
        """
        return try decoder.decode(Tally.Category.self, from: Data(json.utf8))
    }

    private var decoder: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }

    func model(accounts: [Account] = []) -> AddModel {
        let model = AddModel(preferences: preferences)
        model.accounts = accounts
        model.applyDefaultAccount()
        return model
    }

    func type(_ model: AddModel, _ text: String) {
        for character in text {
            if character == "." { model.pressDecimal() } else if let digit = character.wholeNumberValue { model.press(digit) }
        }
    }

    /// Runs `body` with an outbox on a temporary directory that never sends.
    func withOutbox(_ body: (Outbox, OutboxStore) throws -> Void) throws {
        let fixture = TestSession()
        defer { try? fixture.cleanUp() }
        let directory = FileManager.default.temporaryDirectory.appending(path: "add-outbox-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: directory) }
        let api = APIClient(credentials: fixture.make(), urlSession: StubURLProtocol.makeSession())
        let store = try OutboxStore(directory: directory)
        try body(Outbox(store: store, api: api, autoFlush: false), store)
    }

    // MARK: Keypad

    @Test func keypadBuildsAnAmountAndIgnoresASecondDecimalPoint() {
        let model = AddModel(preferences: preferences)
        type(model, "12.5.")
        #expect(model.amountText == "12.5")
        #expect(model.amount == Money(Decimal(string: "12.5")!))
    }

    @Test func keypadIgnoresAThirdFractionDigit() {
        let model = AddModel(preferences: preferences)
        type(model, "1.234")
        #expect(model.amountText == "1.23")
    }

    @Test func keypadBackspaceAndClear() {
        let model = AddModel(preferences: preferences)
        type(model, "4.5")
        model.backspace()
        model.backspace()
        #expect(model.amountText == "4")
        model.clear()
        #expect(model.amountText.isEmpty)
        model.backspace()
        #expect(model.amount == nil)
    }

    @Test func keypadHandlesLeadingZerosAndALoneDecimalPoint() {
        let model = AddModel(preferences: preferences)
        type(model, "007")
        #expect(model.amountText == "7")
        model.clear()
        model.pressDecimal()
        #expect(model.amountText == "0.")
        type(model, "05")
        #expect(model.amountText == "0.05")
        #expect(model.amount == Money(Decimal(string: "0.05")!))
    }

    @Test func zeroAmountIsNotSavable() throws {
        let model = model(accounts: [try account()])
        type(model, "0.00")
        #expect(model.amount == nil)
        #expect(!model.canSave)
        model.clear()
        model.press(1)
        #expect(model.canSave)
    }

    @Test func keypadStopsAtNineIntegerDigits() {
        let model = AddModel(preferences: preferences)
        type(model, "12345678901")
        #expect(model.amountText == "123456789")
    }

    // MARK: Save

    @Test func expenseIsSavedNegativeAndMoneyInPositive() throws {
        let card = try account()
        let model = model(accounts: [card])
        try withOutbox { outbox, _ in
            type(model, "12.50")
            #expect(model.save(outbox: outbox))
            model.direction = .moneyIn
            type(model, "3")
            #expect(model.save(outbox: outbox))
            let amounts = outbox.pending.map(\.capture.amount.amount)
            #expect(Set(amounts) == [Decimal(string: "-12.5")!, 3])
        }
    }

    @Test func descriptionFallsBackToCategoryNameThenManualExpense() throws {
        let groceries = try category(name: "Groceries")
        let model = model(accounts: [try account()])
        model.categories = [groceries]
        try withOutbox { outbox, _ in
            type(model, "5")
            model.categoryId = groceries.id
            model.description = "   "
            #expect(model.save(outbox: outbox))
            type(model, "6")
            #expect(model.save(outbox: outbox))
            let descriptions = outbox.pending.map(\.capture.descriptionRaw)
            #expect(descriptions.sorted() == ["Groceries", "Manual expense"])
        }
    }

    @Test func enqueuedCaptureCarriesTheFormFields() async throws {
        let card = try account()
        let groceries = try category(name: "Groceries")
        let model = model(accounts: [card])
        model.categories = [groceries]
        let jpeg = Data([0xFF, 0xD8, 0xFF])
        let eventId = UUID()
        await model.attachReceipt(jpeg: jpeg)
        try withOutbox { outbox, store in
            type(model, "20")
            model.bucket = .business
            model.categoryId = groceries.id
            model.eventId = eventId
            model.description = "  Lunch  "
            model.merchant = " Cafe "
            model.notes = "  "
            let date = CalendarDate(year: 2026, month: 3, day: 4)
            model.date = date
            #expect(model.save(outbox: outbox))

            let item = try #require(outbox.pending.first)
            let capture = item.capture
            #expect(capture.accountId == card.id)
            #expect(capture.postedDate == date)
            #expect(capture.descriptionRaw == "Lunch")
            #expect(capture.merchant == "Cafe")
            #expect(capture.notes == nil)
            #expect(capture.categoryId == groceries.id)
            #expect(capture.bucket == .business)
            #expect(capture.eventId == eventId)
            #expect(capture.receiptId != nil)
            #expect(try store.receiptJPEG(for: item.id) == jpeg)
            #expect(model.saveError == nil)
        }
    }

    @Test func saveResetsTheEntryButKeepsCardBucketAndToday() throws {
        let card = try account()
        let model = model(accounts: [card])
        try withOutbox { outbox, _ in
            type(model, "9")
            model.bucket = .reimbursable
            model.direction = .moneyIn
            model.merchant = "x"
            model.date = CalendarDate(year: 2020, month: 1, day: 1)
            #expect(model.save(outbox: outbox))
            #expect(model.amountText.isEmpty)
            #expect(model.merchant.isEmpty)
            #expect(model.direction == .expense)
            #expect(model.bucket == .reimbursable)
            #expect(model.accountId == card.id)
            #expect(model.date == AddModel(preferences: preferences).date)
        }
    }

    @Test func saveClearsTheAttachedReceipt() async throws {
        let model = model(accounts: [try account()])
        await model.attachReceipt(jpeg: Data([0xFF, 0xD8, 0xFF, 0xD9]))
        type(model, "4")
        try withOutbox { outbox, _ in
            #expect(model.save(outbox: outbox))
        }
        #expect(model.receiptJPEG == nil)
        #expect(model.receiptThumbnail == nil)
    }

    @Test func nextModelPreselectsTheLastUsedCardAndBucket() throws {
        let first = try account(name: "First")
        let second = try account(name: "Second")
        let model = model(accounts: [first, second])
        model.selectAccount(second.id)
        model.bucket = .business
        try withOutbox { outbox, _ in
            type(model, "1")
            #expect(model.save(outbox: outbox))
        }
        let next = self.model(accounts: [first, second])
        #expect(next.accountId == second.id)
        #expect(next.bucket == .business)
    }

    @Test func lastUsedCardThatIsNoLongerActiveFallsBackToFirstActive() throws {
        let gone = UUID()
        preferences.record(accountId: gone, bucket: .personal)
        let live = try account(name: "Live")
        let next = model(accounts: [try account(id: gone, active: false), live])
        #expect(next.accountId == live.id)
    }

    @Test func signOutClearsTheLastUsedCardAndBucket() throws {
        let fixture = TestSession()
        defer { try? fixture.cleanUp() }
        let prefs = CapturePreferences(defaults: fixture.defaults)
        prefs.record(accountId: UUID(), bucket: .business)
        #expect(prefs.accountId != nil)
        fixture.make().signOut()
        #expect(prefs.accountId == nil)
        #expect(prefs.bucket == nil)
    }

    @Test func failedEnqueueKeepsTheFormAndSetsTheError() throws {
        let fixture = TestSession()
        defer { try? fixture.cleanUp() }
        let api = APIClient(credentials: fixture.make(), urlSession: StubURLProtocol.makeSession())
        let outbox = Outbox(unavailableBecause: "no disk", api: api)
        let model = model(accounts: [try account()])
        type(model, "7")
        model.merchant = "Cafe"
        #expect(!model.save(outbox: outbox))
        #expect(model.amountText == "7")
        #expect(model.merchant == "Cafe")
        #expect(model.saveError == OutboxError.storageUnavailable.localizedDescription)
        #expect(preferences.accountId == nil)
    }

    // MARK: Fields

    @Test func changingDirectionClearsACategoryThatNoLongerFits() throws {
        let groceries = try category(name: "Groceries")
        let salary = try category(name: "Salary", kind: "income")
        let model = model(accounts: [try account()])
        model.categories = [groceries, salary]
        model.categoryId = groceries.id
        model.direction = .moneyIn
        #expect(model.categoryId == nil)
        model.categoryId = salary.id
        model.direction = .moneyIn
        #expect(model.categoryId == salary.id)
        model.direction = .expense
        #expect(model.categoryId == nil)
    }

    @Test func aCardPickedBeforeTheLoadFinishesIsKept() throws {
        let first = try account(name: "First")
        let second = try account(name: "Second")
        preferences.record(accountId: first.id, bucket: .personal)
        let model = AddModel(preferences: preferences)
        model.selectAccount(second.id)
        model.accounts = [first, second]
        model.applyDefaultAccount()
        #expect(model.accountId == second.id)
    }

    @Test func theDatePickerUsesTheModelsCalendar() {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Pacific/Auckland")!
        let model = AddModel(preferences: preferences, calendar: calendar)
        let picked = calendar.date(from: DateComponents(year: 2026, month: 3, day: 4, hour: 23))!
        model.dateValue = picked
        #expect(model.date == CalendarDate(year: 2026, month: 3, day: 4))
        #expect(model.dateValue == calendar.date(from: DateComponents(year: 2026, month: 3, day: 4)))
    }

    // MARK: Receipt

    private func png() -> Data {
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        return UIGraphicsImageRenderer(size: CGSize(width: 400, height: 300), format: format).pngData { context in
            UIColor.blue.setFill()
            context.fill(CGRect(x: 0, y: 0, width: 400, height: 300))
        }
    }

    @Test func attachingAPhotoStoresAJPEGAndAThumbnail() async {
        let model = AddModel(preferences: preferences)
        await model.attachReceipt(imageData: png())
        #expect(model.receiptJPEG.map { Array($0.prefix(2)) } == [0xFF, 0xD8])
        #expect(model.receiptThumbnail != nil)
        #expect(model.receiptError == nil)
    }

    @Test func aPhotoThatIsNotAnImageSetsAnErrorAndAttachesNothing() async {
        let model = AddModel(preferences: preferences)
        await model.attachReceipt(imageData: Data("nope".utf8))
        #expect(model.receiptJPEG == nil)
        #expect(model.receiptError != nil)
        await model.attachReceipt(imageData: png())
        #expect(model.receiptError == nil)
    }

    @Test func removingTheReceiptClearsTheJPEGAndThumbnail() async {
        let model = AddModel(preferences: preferences)
        await model.attachReceipt(imageData: png())
        model.removeReceipt()
        #expect(model.receiptJPEG == nil)
        #expect(model.receiptThumbnail == nil)
    }

    private func scannedPage() -> CGImage {
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        return UIGraphicsImageRenderer(size: CGSize(width: 300, height: 500), format: format).image { context in
            UIColor.white.setFill()
            context.fill(CGRect(x: 0, y: 0, width: 300, height: 500))
        }.cgImage!
    }

    @Test func attachingAScanStoresOneJPEGAndAThumbnail() async {
        let model = AddModel(preferences: preferences)
        await model.attachReceipt(scannedPages: [scannedPage(), scannedPage()])
        #expect(model.receiptJPEG.map { Array($0.prefix(2)) } == [0xFF, 0xD8])
        #expect(model.receiptJPEG.flatMap { UIImage(data: $0) }?.size == CGSize(width: 300, height: 1000))
        #expect(model.receiptThumbnail != nil)
        #expect(model.receiptError == nil)
    }

    @Test func aScanWithNoPagesSetsAnErrorAndAttachesNothing() async {
        let model = AddModel(preferences: preferences)
        await model.attachReceipt(scannedPages: [])
        #expect(model.receiptJPEG == nil)
        #expect(model.receiptError != nil)
    }
}
