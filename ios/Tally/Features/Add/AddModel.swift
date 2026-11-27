import Foundation
import Observation
import UIKit

/// The state behind the Add tab: a keypad amount and the fields of one manual expense (or money in),
/// saved to the outbox with the last-used card and bucket preselected.
@Observable
final class AddModel {
    typealias Direction = ManualCapture.Direction

    private static let maxIntegerDigits = 9
    private static let maxFractionDigits = 2
    private static let thumbnailSize = CGSize(width: 132, height: 132)

    /// What has been typed on the keypad, e.g. `""`, `"12"`, `"0."`, `"12.5"`.
    private(set) var amountText = ""
    var direction: Direction = .expense {
        didSet {
            if direction != oldValue, let id = categoryId, !availableCategories.contains(where: { $0.id == id }) {
                categoryId = nil
            }
        }
    }
    var bucket: Bucket
    var accountId: UUID?
    var categoryId: UUID?
    var eventId: UUID?
    var date: CalendarDate
    var description = ""
    var merchant = ""
    var notes = ""

    /// A plain-language note for AI quick capture (#171), e.g. "client lunch Chipotle 18.40 business".
    var note = ""
    private(set) var isReadingNote = false
    /// Why the note couldn't be read; the form is left as it was.
    private(set) var noteError: String?

    /// The receipt to send with the capture, and a small preview of it.
    private(set) var receiptJPEG: Data?
    private(set) var receiptThumbnail: UIImage?
    /// Why a receipt couldn't be attached.
    var receiptError: String?

    var accounts: [Account] = []
    var categories: [Category] = []
    var events: [Event] = []
    private(set) var hasLoaded = false
    /// Why the reference data couldn't be loaded.
    private(set) var loadError: String?
    /// Why the last save failed; the form is left as it was.
    private(set) var saveError: String?

    @ObservationIgnored private let preferences: CapturePreferences
    @ObservationIgnored private let calendar: Calendar
    /// Set once the user touches the card, so a late load doesn't overwrite their choice.
    @ObservationIgnored private var accountIsDefault = true

    init(preferences: CapturePreferences = CapturePreferences(), calendar: Calendar = .current, now: Date = .now) {
        self.preferences = preferences
        self.calendar = calendar
        self.bucket = preferences.bucket ?? .personal
        self.date = CalendarDate(date: now, calendar: calendar)
    }

    // MARK: Keypad

    func press(_ digit: Int) {
        guard (0...9).contains(digit) else { return }
        if let point = amountText.firstIndex(of: ".") {
            guard amountText.distance(from: point, to: amountText.endIndex) - 1 < Self.maxFractionDigits else { return }
            amountText.append(String(digit))
        } else if amountText == "0" {
            amountText = String(digit)
        } else if amountText.count < Self.maxIntegerDigits {
            amountText.append(String(digit))
        }
    }

    func pressDecimal() {
        guard !amountText.contains(".") else { return }
        amountText = amountText.isEmpty ? "0." : amountText + "."
    }

    func backspace() {
        amountText = String(amountText.dropLast())
    }

    func clear() {
        amountText = ""
    }

    /// The typed amount, or nil while nothing (or only zero) is typed.
    var amount: Money? {
        let digits = amountText.hasSuffix(".") ? String(amountText.dropLast()) : amountText
        guard let value = Decimal(string: digits, locale: Locale(identifier: "en_US_POSIX")), value > 0 else { return nil }
        return Money(value)
    }

    // MARK: Fields

    var activeAccounts: [Account] { accounts.filter(\.active) }

    /// Categories that fit the direction: expense categories for an expense, income ones for money in.
    var availableCategories: [Category] {
        let kind: CategoryKind = direction == .expense ? .expense : .income
        return categories.pickable(keeping: categoryId).filter { $0.kind == kind }
    }

    /// Adds a category made on this screen and selects it.
    func addCategory(_ category: Category) {
        categories.append(category)
        categoryId = category.id
    }

    var canSave: Bool { amount != nil && accountId != nil }

    /// `date` as a `Date` in the model's calendar, for a date picker.
    var dateValue: Date {
        get { date.date(in: calendar) ?? .now }
        set { date = CalendarDate(date: newValue, calendar: calendar) }
    }

    func selectAccount(_ id: UUID) {
        accountId = id
        accountIsDefault = false
    }

    /// The last-used card if it is still active, else the first active one; a card the user picked is kept.
    func applyDefaultAccount() {
        let active = activeAccounts
        if !accountIsDefault, let current = accountId, active.contains(where: { $0.id == current }) { return }
        accountIsDefault = true
        if let last = preferences.accountId, active.contains(where: { $0.id == last }) {
            accountId = last
        } else {
            accountId = active.first?.id
        }
    }

    // MARK: Quick capture

    /// Sends `note` to the server and fills the form from the draft for review. Saves nothing.
    func readNote(api: APIClient) async {
        guard let text = note.trimmedOrNil, !isReadingNote else { return }
        isReadingNote = true
        noteError = nil
        defer { isReadingNote = false }
        do {
            apply(try await api.parseExpense(text))
            note = ""
        } catch is CancellationError {
            return
        } catch {
            noteError = error.localizedDescription
        }
    }

    /// Fills the fields the draft has; the rest keep what's there. A category that doesn't fit the direction is left out.
    func apply(_ draft: ExpenseDraft) {
        direction = draft.direction == .income ? .moneyIn : .expense
        if let amount = draft.amount, amount.amount > 0 { amountText = amount.inputText }
        if let merchant = draft.merchant { self.merchant = merchant }
        if let description = draft.description { self.description = description }
        date = draft.date
        if let bucket = draft.bucket { self.bucket = bucket }
        if let id = draft.categoryId, availableCategories.contains(where: { $0.id == id }) { categoryId = id }
    }

    // MARK: Receipt

    /// Converts a picked photo (HEIC, PNG, JPEG) to a receipt JPEG and attaches it; sets `receiptError` when it can't be converted.
    func attachReceipt(imageData: Data) async {
        receiptError = nil
        guard let jpeg = await Task.detached(operation: { ReceiptImage.jpeg(from: imageData) }).value else {
            receiptError = "That photo couldn't be converted to JPEG."
            return
        }
        await attachReceipt(jpeg: jpeg)
    }

    /// Stacks the document camera's pages into one receipt JPEG and attaches it; sets `receiptError` when the
    /// scan has no usable page.
    func attachReceipt(scannedPages pages: [CGImage]) async {
        receiptError = nil
        guard let jpeg = await Task.detached(operation: { ReceiptImage.jpeg(stacking: pages) }).value else {
            receiptError = "Couldn't read the scan."
            return
        }
        await attachReceipt(jpeg: jpeg)
    }

    /// Attaches receipt JPEG bytes as they are.
    func attachReceipt(jpeg: Data) async {
        receiptError = nil
        let size = Self.thumbnailSize
        receiptThumbnail = await Task.detached(operation: { UIImage(data: jpeg)?.preparingThumbnail(of: size) }).value
        receiptJPEG = jpeg
    }

    func removeReceipt() {
        receiptJPEG = nil
        receiptThumbnail = nil
    }

    // MARK: Load and save

    /// Loads the cards, categories and events for the pickers, then picks the default card.
    /// A list that fails to load leaves the others populated; `loadError` carries the first failure.
    func load(api: APIClient) async {
        async let accounts = Self.result { try await api.accounts() }
        async let categories = Self.result { try await api.categories() }
        async let events = Self.result { try await api.events() }
        let loaded = await (accounts, categories, events)
        let failures = [
            take(loaded.0, into: \.accounts),
            take(loaded.1, into: \.categories),
            take(loaded.2, into: \.events),
        ]
        loadError = failures.compactMap { $0 }.first
        hasLoaded = true
        applyDefaultAccount()
    }

    /// Queues the expense in the outbox. Returns whether it was saved; on failure every field is kept
    /// and `saveError` says why.
    func save(outbox: Outbox) -> Bool {
        guard let entry else { return false }
        do {
            try outbox.enqueue(entry.capture(), receiptJPEG: receiptJPEG)
        } catch {
            saveError = error.localizedDescription
            return false
        }
        saveError = nil
        preferences.record(accountId: entry.accountId, bucket: bucket)
        resetForNext()
        return true
    }

    // MARK: Private

    private var entry: ManualCapture? {
        guard let amount, let accountId else { return nil }
        return ManualCapture(
            amount: amount,
            direction: direction,
            accountId: accountId,
            bucket: bucket,
            categoryId: categoryId,
            categoryName: categories.first { $0.id == categoryId }?.name,
            eventId: eventId,
            date: date,
            description: description,
            merchant: merchant,
            notes: notes
        )
    }

    private func resetForNext() {
        amountText = ""
        description = ""
        merchant = ""
        notes = ""
        categoryId = nil
        eventId = nil
        removeReceipt()
        direction = .expense
        date = CalendarDate(date: .now, calendar: calendar)
    }

    /// Stores a loaded list, or returns why it failed.
    private func take<T>(_ result: Result<T, any Error>, into keyPath: ReferenceWritableKeyPath<AddModel, T>) -> String? {
        switch result {
        case .success(let value):
            self[keyPath: keyPath] = value
            return nil
        case .failure(let error):
            return error.localizedDescription
        }
    }

    private static func result<T>(_ work: () async throws -> T) async -> Result<T, any Error> {
        do { return .success(try await work()) } catch { return .failure(error) }
    }
}
