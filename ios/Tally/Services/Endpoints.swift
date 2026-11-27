import Foundation
import os

extension APIClient {
    /// Request timeout for the Claude-backed calls that answer in one go; chat gets longer for its tool loop.
    private static let aiTimeout: TimeInterval = 120
    private static let chatTimeout: TimeInterval = 180

    /// `POST /api/devices`: registers this phone's push token, or refreshes it when the server already has it.
    func registerDevice(_ registration: DeviceRegistration) async throws -> RegisteredDevice {
        try await send("POST", "/api/devices", body: registration)
    }

    /// `GET /api/ai/usage`.
    func aiUsage() async throws -> AIUsage {
        try await get("/api/ai/usage")
    }

    /// `POST /api/ai/parse-expense`: a draft from a note; 503 when AI is off or over budget.
    func parseExpense(_ text: String) async throws -> ExpenseDraft {
        try await send("POST", "/api/ai/parse-expense", body: ParseExpenseBody(text: text), timeout: Self.aiTimeout)
    }

    /// `POST /api/ai/guess-mapping`: a CSV column mapping guessed from the file's first records, to review.
    func guessImportMapping(records: [[String]]) async throws -> ImportMapping {
        try await send("POST", "/api/ai/guess-mapping", body: GuessMappingBody(records: records), timeout: Self.aiTimeout)
    }

    /// `GET /api/ai/weekly-review`: the newest weekly review, or nil before the first one.
    func weeklyReview() async throws -> WeeklyReview? {
        try await get("/api/ai/weekly-review")
    }

    /// `POST /api/ai/chat`: one question in a money chat; 503 when AI is off, 409 when the chat is full.
    func chat(conversationId: UUID, message: String) async throws -> ChatReply {
        try await send("POST", "/api/ai/chat", body: ChatBody(conversationId: conversationId, message: message), timeout: Self.chatTimeout)
    }

    /// `GET /api/ai/actions`: AI-proposed actions, newest first, optionally only those in `status`.
    func aiActions(status: AIActionStatus? = nil) async throws -> [AIAction] {
        let query = status.map { [URLQueryItem(name: "status", value: $0.rawValue)] } ?? []
        return try await get("/api/ai/actions", query: query)
    }

    /// `GET /api/ai/actions/{id}`.
    func aiAction(id: UUID) async throws -> AIAction {
        try await get("/api/ai/actions/\(id.uuidString.lowercased())")
    }

    /// `POST /api/ai/actions/{id}/confirm`: applies every step at once; 409 with the server's reason when it can't.
    func confirmAIAction(id: UUID) async throws -> AIAction {
        try await send("POST", "/api/ai/actions/\(id.uuidString.lowercased())/confirm", body: nil as SimplefinClaimBody?)
    }

    /// `POST /api/ai/actions/{id}/dismiss`.
    func dismissAIAction(id: UUID) async throws -> AIAction {
        try await send("POST", "/api/ai/actions/\(id.uuidString.lowercased())/dismiss", body: nil as SimplefinClaimBody?)
    }

    /// `POST /api/ai/actions/{id}/undo`: 409 with the server's reason when something changed since.
    func undoAIAction(id: UUID) async throws -> AIAction {
        try await send("POST", "/api/ai/actions/\(id.uuidString.lowercased())/undo", body: nil as SimplefinClaimBody?)
    }

    /// `GET /api/insights`: the current insights, newest first.
    func insights() async throws -> [Insight] {
        try await get("/api/insights")
    }

    /// `POST /api/insights/{id}/dismiss`.
    func dismissInsight(id: UUID) async throws -> Insight {
        try await send("POST", "/api/insights/\(id.uuidString.lowercased())/dismiss", body: nil as SimplefinClaimBody?)
    }

    /// `GET /api/ai/tax-review?year=`: the year's AI review and its state, or nil before one is asked for.
    func taxReview(year: Int) async throws -> TaxReview? {
        try await get("/api/ai/tax-review", query: [URLQueryItem(name: "year", value: String(year))])
    }

    /// `POST /api/ai/tax-review`: asks the server to write the year's review; 409 while one is being written.
    func requestTaxReview(year: Int) async throws -> TaxReview {
        try await send("POST", "/api/ai/tax-review", body: TaxReviewBody(year: year), timeout: Self.aiTimeout)
    }

    /// `GET /api/home?month=YYYY-MM`.
    func home(month: MonthRef) async throws -> Home {
        try await get("/api/home", query: [URLQueryItem(name: "month", value: month.apiString)])
    }

    /// `GET /api/dashboard?month=YYYY-MM[&months=N]`; `months` is left out when it is the server's default.
    func dashboard(month: MonthRef, months: Int = Dashboard.defaultTrendMonths) async throws -> Dashboard {
        var query = [URLQueryItem(name: "month", value: month.apiString)]
        if months != Dashboard.defaultTrendMonths {
            query.append(URLQueryItem(name: "months", value: String(months)))
        }
        return try await get("/api/dashboard", query: query)
    }
}

/// Filters for `GET /api/transactions`; unset fields are left out of the query.
struct TransactionQuery {
    var month: MonthRef?
    var search: String?
    var bucket: Bucket?
    var accountId: UUID?
    var categoryId: UUID?
    var reviewed: Bool?
    var reimbursementId: UUID?
    var excludeSplitParents: Bool?
    var limit: Int?
    var cursor: String?

    var queryItems: [URLQueryItem] {
        [
            month.map { URLQueryItem(name: "month", value: $0.apiString) },
            search.map { URLQueryItem(name: "q", value: $0) },
            bucket.map { URLQueryItem(name: "bucket", value: $0.rawValue) },
            accountId.map { URLQueryItem(name: "account_id", value: $0.uuidString.lowercased()) },
            categoryId.map { URLQueryItem(name: "category_id", value: $0.uuidString.lowercased()) },
            reviewed.map { URLQueryItem(name: "reviewed", value: $0 ? "true" : "false") },
            reimbursementId.map { URLQueryItem(name: "reimbursement_id", value: $0.uuidString.lowercased()) },
            excludeSplitParents.map { URLQueryItem(name: "exclude_split_parents", value: $0 ? "true" : "false") },
            limit.map { URLQueryItem(name: "limit", value: String($0)) },
            cursor.map { URLQueryItem(name: "cursor", value: $0) },
        ].compactMap { $0 }
    }
}

extension APIClient {
    /// `GET /api/inbox?limit=&cursor=`.
    func inbox(limit: Int? = nil, cursor: String? = nil) async throws -> InboxPage {
        let query = [
            limit.map { URLQueryItem(name: "limit", value: String($0)) },
            cursor.map { URLQueryItem(name: "cursor", value: $0) },
        ].compactMap { $0 }
        return try await get("/api/inbox", query: query)
    }

    /// `GET /api/transactions` with the filters in `query`.
    func transactions(_ query: TransactionQuery = TransactionQuery()) async throws -> TransactionPage {
        try await get("/api/transactions", query: query.queryItems)
    }

    /// `GET /api/transactions?format=csv` with the list's filters; every matching row as CSV bytes.
    func transactionsCSV(_ query: TransactionQuery) async throws -> Data {
        try await download("/api/transactions", query: query.queryItems + [URLQueryItem(name: "format", value: "csv")])
    }

    /// Every row of `GET /api/transactions` for `query`'s filters, in server order: the largest pages, following
    /// `next_cursor` until it runs out. Stops after `maxPages` pages, logging that later rows were left out.
    func allTransactions(_ query: TransactionQuery, maxPages: Int = 20) async throws -> [Transaction] {
        var query = query
        query.limit = TransactionPage.maxLimit
        query.cursor = nil
        var rows: [Transaction] = []
        for _ in 0..<maxPages {
            let page = try await transactions(query)
            rows += page.items
            guard let next = page.nextCursor else { return rows }
            query.cursor = next
        }
        Self.pagingLogger.error("Transactions stopped at \(maxPages, privacy: .public) pages; later rows left out")
        return rows
    }

    private static let pagingLogger = Logger(subsystem: "com.luisd07.tally", category: "Endpoints")

    /// `GET /api/tax/missing-receipts?year=&bucket=[&limit=][&cursor=]`. The bucket is always sent, though the
    /// server defaults to business, so the request says which list it is for.
    func missingReceipts(year: Int, bucket: ReceiptBucket, limit: Int? = nil, cursor: String? = nil) async throws -> MissingReceiptPage {
        let query = [
            URLQueryItem(name: "year", value: String(year)),
            URLQueryItem(name: "bucket", value: bucket.bucket.rawValue),
            limit.map { URLQueryItem(name: "limit", value: String($0)) },
            cursor.map { URLQueryItem(name: "cursor", value: $0) },
        ].compactMap { $0 }
        return try await get("/api/tax/missing-receipts", query: query)
    }

    /// `GET /api/tax/summary?year=`.
    func taxSummary(year: Int) async throws -> TaxSummary {
        try await get("/api/tax/summary", query: [URLQueryItem(name: "year", value: String(year))])
    }

    /// `GET /api/tax/export?year=`; the year's zip bytes.
    func taxExport(year: Int) async throws -> Data {
        try await download("/api/tax/export", query: [URLQueryItem(name: "year", value: String(year))])
    }

    /// `GET /api/auth/events`, newest first: sign-ins, refreshes and refused attempts.
    func authEvents(limit: Int = 100, cursor: String? = nil) async throws -> AuthEventPage {
        var query = [URLQueryItem(name: "limit", value: String(limit))]
        if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
        return try await get("/api/auth/events", query: query)
    }

    /// `POST /api/sync/simplefin/claim`; trades a SimpleFIN setup token for the server's stored connection.
    func claimSimplefin(setupToken: String) async throws -> SimplefinClaim {
        try await send("POST", "/api/sync/simplefin/claim", body: SimplefinClaimBody(setupToken: setupToken))
    }

    /// `POST /api/sync/simplefin/run`; syncs now and returns the finished run. The first sync backfills the year.
    func syncNow() async throws -> SyncRun {
        try await send("POST", "/api/sync/simplefin/run", body: nil as SimplefinClaimBody?)
    }

    /// `GET /api/sync/runs`, newest first.
    func syncRuns(limit: Int = 10) async throws -> SyncRunPage {
        try await get("/api/sync/runs", query: [URLQueryItem(name: "limit", value: String(limit))])
    }

    /// `GET /api/categories`, archived ones included.
    func categories() async throws -> [Category] {
        try await get("/api/categories")
    }

    /// `POST /api/categories`; replaying a draft's id returns the original category.
    func createCategory(_ draft: CategoryDraft) async throws -> Category {
        try await send("POST", "/api/categories", body: draft)
    }

    /// `PATCH /api/categories/{id}`; returns the updated category.
    func updateCategory(id: UUID, patch: CategoryPatch) async throws -> Category {
        try await send("PATCH", "/api/categories/\(id.uuidString.lowercased())", body: patch)
    }

    /// `GET /api/events?year=`; every event when `year` is nil.
    func events(year: Int? = nil) async throws -> [Event] {
        try await get("/api/events", query: year.map { [URLQueryItem(name: "year", value: String($0))] } ?? [])
    }

    /// `GET /api/accounts`.
    func accounts() async throws -> [Account] {
        try await get("/api/accounts")
    }

    /// `POST /api/transactions`; replaying a capture's id returns the original row.
    func createTransaction(_ capture: Capture) async throws -> Transaction {
        try await send("POST", "/api/transactions", body: capture.createBody)
    }

    /// `PATCH /api/transactions/{id}`; returns the updated row.
    func updateTransaction(id: UUID, patch: TransactionPatch) async throws -> Transaction {
        try await send("PATCH", "/api/transactions/\(id.uuidString.lowercased())", body: patch)
    }

    /// `POST /api/transactions/bulk`; applies `patch` to every row in `ids`, or to none when any id is unknown.
    func bulkUpdate(ids: [UUID], patch: TransactionPatch) async throws -> BulkResult {
        try await send("POST", "/api/transactions/bulk", body: TransactionBulkUpdate(ids: ids, patch: patch))
    }

    /// `POST /api/transactions/{id}/split`; replaces the transaction's children when it is already split.
    func splitTransaction(id: UUID, parts: [SplitPart]) async throws -> SplitResult {
        try await send("POST", "/api/transactions/\(id.uuidString.lowercased())/split", body: SplitRequest(parts: parts))
    }

    /// `GET /api/transactions/{id}/receipts`: the transaction's receipts with their readings.
    func receipts(transactionId: UUID) async throws -> [ReceiptWithReading] {
        try await get("/api/transactions/\(transactionId.uuidString.lowercased())/receipts")
    }

    /// `POST /api/transactions/{id}/receipts`; replaying the same `receiptId` returns the existing row.
    func uploadReceipt(transactionId: UUID, jpeg: Data, receiptId: UUID) async throws -> Receipt {
        try await sendMultipart(
            "POST",
            "/api/transactions/\(transactionId.uuidString.lowercased())/receipts",
            parts: [
                .field("receipt_id", receiptId.uuidString.lowercased()),
                .file("file", filename: "receipt.jpg", contentType: "image/jpeg", data: jpeg),
            ]
        )
    }
}

extension APIClient {
    /// `GET /api/income[?month=YYYY-MM][&type=]`; every month's entries when `month` is nil.
    func income(month: MonthRef? = nil, type: IncomeType? = nil) async throws -> IncomePage {
        let query = [
            month.map { URLQueryItem(name: "month", value: $0.apiString) },
            type.map { URLQueryItem(name: "type", value: $0.rawValue) },
        ].compactMap { $0 }
        return try await get("/api/income", query: query)
    }

    /// `POST /api/income`; replaying a draft's id returns the original row.
    func createIncome(_ draft: IncomeDraft) async throws -> IncomeEntry {
        try await send("POST", "/api/income", body: draft)
    }

    /// `PATCH /api/income/{id}`; returns the updated row.
    func updateIncome(id: UUID, patch: IncomePatch) async throws -> IncomeEntry {
        try await send("PATCH", "/api/income/\(id.uuidString.lowercased())", body: patch)
    }
}

extension APIClient {
    /// `include_inactive=true` when `include` is true, else nothing: a list endpoint's default is active rows only.
    private static func includeInactiveQuery(_ include: Bool) -> [URLQueryItem] {
        include ? [URLQueryItem(name: "include_inactive", value: "true")] : []
    }

    /// `GET /api/subscriptions`; closed ones are listed too when `includeInactive` is true.
    func subscriptions(includeInactive: Bool = false) async throws -> SubscriptionPage {
        try await get("/api/subscriptions", query: Self.includeInactiveQuery(includeInactive))
    }

    /// `POST /api/subscriptions`; replaying a draft's id returns the original row.
    func createSubscription(_ draft: SubscriptionDraft) async throws -> Subscription {
        try await send("POST", "/api/subscriptions", body: draft)
    }

    /// `PATCH /api/subscriptions/{id}`; returns the updated row.
    func updateSubscription(id: UUID, patch: SubscriptionPatch) async throws -> Subscription {
        try await send("PATCH", "/api/subscriptions/\(id.uuidString.lowercased())", body: patch)
    }
}

extension APIClient {
    /// `GET /api/accounts/cards`; closed cards are listed too when `includeInactive` is true.
    func cards(includeInactive: Bool = false) async throws -> [CardSummary] {
        try await get("/api/accounts/cards", query: Self.includeInactiveQuery(includeInactive))
    }

    /// `GET /api/accounts/loans`; every active loan with its payoff.
    func loans() async throws -> [LoanSummary] {
        try await get("/api/accounts/loans")
    }

    /// `GET /api/accounts/interest`; interest and fees this year across every card, closed ones too.
    func interestSummary() async throws -> InterestSummary {
        try await get("/api/accounts/interest")
    }

    /// `GET /api/accounts/payoff`; the strategies use `budget` a month, the sum of the planned payments when nil.
    func payoffPlan(budget: Money? = nil) async throws -> PayoffPlan {
        try await get("/api/accounts/payoff", query: budget.map { [URLQueryItem(name: "budget", value: $0.inputText)] } ?? [])
    }

    /// `GET /api/accounts/{id}/card-log?month=YYYY-MM`; a credit card's month reconciled against its balances.
    func cardLog(accountId: UUID, month: MonthRef) async throws -> CardLog {
        try await get(
            "/api/accounts/\(accountId.uuidString.lowercased())/card-log",
            query: [URLQueryItem(name: "month", value: month.apiString)]
        )
    }

    /// `PATCH /api/accounts/{id}`; returns the updated account.
    func updateAccount(id: UUID, patch: AccountPatch) async throws -> Account {
        try await send("PATCH", "/api/accounts/\(id.uuidString.lowercased())", body: patch)
    }

    /// `PUT /api/accounts/{id}/balances/{date}`; records a manual month-end balance (negative for debt),
    /// replacing any for that day.
    func upsertBalance(accountId: UUID, date: CalendarDate, balance: Money) async throws -> BalanceSnapshot {
        try await send(
            "PUT",
            "/api/accounts/\(accountId.uuidString.lowercased())/balances/\(date.apiString)",
            body: BalanceUpsert(balance: balance)
        )
    }
}

extension APIClient {
    /// `GET /api/reimbursements[?status=]`; every report, newest first, when `status` is nil.
    func reimbursements(status: ReimbursementStatus? = nil) async throws -> ReimbursementPage {
        let query = status.map { [URLQueryItem(name: "status", value: $0.rawValue)] } ?? []
        return try await get("/api/reimbursements", query: query)
    }

    /// `GET /api/reimbursements/owed`; what is still owed back to you, the number Home shows.
    func owedToYou() async throws -> Owed {
        try await get("/api/reimbursements/owed")
    }

    /// `POST /api/reimbursements`; replaying a draft's id returns the original report.
    func createReimbursement(_ draft: ReimbursementDraft) async throws -> Reimbursement {
        try await send("POST", "/api/reimbursements", body: draft)
    }

    /// `PATCH /api/reimbursements/{id}`; returns the updated report.
    func updateReimbursement(id: UUID, patch: ReimbursementPatch) async throws -> Reimbursement {
        try await send("PATCH", "/api/reimbursements/\(id.uuidString.lowercased())", body: patch)
    }
}

extension APIClient {
    /// `GET /api/rules`; every rule, active or not, in the order they run.
    func rules() async throws -> [Rule] {
        try await get("/api/rules")
    }

    /// `POST /api/rules`; replaying a draft's id returns the original rule.
    func createRule(_ draft: RuleDraft) async throws -> Rule {
        try await send("POST", "/api/rules", body: draft)
    }

    /// `PATCH /api/rules/{id}`; returns the updated rule.
    func updateRule(id: UUID, patch: RulePatch) async throws -> Rule {
        try await send("PATCH", "/api/rules/\(id.uuidString.lowercased())", body: patch)
    }

    /// `DELETE /api/rules/{id}`, sent without a body.
    func deleteRule(id: UUID) async throws {
        try await delete("/api/rules/\(id.uuidString.lowercased())")
    }
}

extension APIClient {
    /// `GET /api/mileage/trips[?year=]`; newest first, every year's trips when `year` is nil.
    func trips(year: Int? = nil) async throws -> [Trip] {
        try await get("/api/mileage/trips", query: year.map { [URLQueryItem(name: "year", value: String($0))] } ?? [])
    }

    /// `POST /api/mileage/trips`; replaying a draft's id returns the original trip.
    func createTrip(_ draft: TripDraft) async throws -> Trip {
        try await send("POST", "/api/mileage/trips", body: draft)
    }

    /// `PATCH /api/mileage/trips/{id}`; returns the updated trip.
    func updateTrip(id: UUID, patch: TripPatch) async throws -> Trip {
        try await send("PATCH", "/api/mileage/trips/\(id.uuidString.lowercased())", body: patch)
    }

    /// `DELETE /api/mileage/trips/{id}`, sent without a body.
    func deleteTrip(id: UUID) async throws {
        try await delete("/api/mileage/trips/\(id.uuidString.lowercased())")
    }

    /// `GET /api/mileage/saved-trips`; by name.
    func savedTrips() async throws -> [SavedTrip] {
        try await get("/api/mileage/saved-trips")
    }

    /// `POST /api/mileage/saved-trips`; replaying a draft's id returns the original saved trip.
    func createSavedTrip(_ draft: SavedTripDraft) async throws -> SavedTrip {
        try await send("POST", "/api/mileage/saved-trips", body: draft)
    }

    /// `PATCH /api/mileage/saved-trips/{id}`; returns the updated saved trip.
    func updateSavedTrip(id: UUID, patch: SavedTripPatch) async throws -> SavedTrip {
        try await send("PATCH", "/api/mileage/saved-trips/\(id.uuidString.lowercased())", body: patch)
    }

    /// `DELETE /api/mileage/saved-trips/{id}`, sent without a body.
    func deleteSavedTrip(id: UUID) async throws {
        try await delete("/api/mileage/saved-trips/\(id.uuidString.lowercased())")
    }

    /// `POST /api/mileage/saved-trips/{id}/log`; logs the saved trip dated today as trip `tripId`. Replaying the
    /// same `tripId` returns the trip already logged.
    func logSavedTrip(id: UUID, tripId: UUID) async throws -> Trip {
        try await send("POST", "/api/mileage/saved-trips/\(id.uuidString.lowercased())/log", body: SavedTripLog(id: tripId))
    }

    /// `GET /api/mileage/odometer/{year}`; nil when the year has no reading (a 404).
    func odometer(year: Int) async throws -> OdometerReading? {
        do {
            return try await get("/api/mileage/odometer/\(year)")
        } catch let error as APIError where error.status == 404 {
            return nil
        }
    }

    /// `PUT /api/mileage/odometer/{year}`; replaces the year's reading and returns it as saved.
    func saveOdometer(year: Int, _ reading: OdometerInput) async throws -> OdometerReading {
        try await send("PUT", "/api/mileage/odometer/\(year)", body: reading)
    }

    /// `GET /api/mileage/rates`; oldest first.
    func mileageRates() async throws -> [MileageRate] {
        try await get("/api/mileage/rates")
    }

    /// `POST /api/mileage/rates`; replaying a draft's id returns the original rate. A rate already on that date is a 409.
    func createMileageRate(_ draft: MileageRateDraft) async throws -> MileageRate {
        try await send("POST", "/api/mileage/rates", body: draft)
    }

    /// `PATCH /api/mileage/rates/{id}`; returns the updated rate.
    func updateMileageRate(id: UUID, patch: MileageRatePatch) async throws -> MileageRate {
        try await send("PATCH", "/api/mileage/rates/\(id.uuidString.lowercased())", body: patch)
    }

    /// `DELETE /api/mileage/rates/{id}`, sent without a body.
    func deleteMileageRate(id: UUID) async throws {
        try await delete("/api/mileage/rates/\(id.uuidString.lowercased())")
    }
}

extension APIClient {
    /// `GET /api/imports/mappings/{account_id}`; nil when the account has no saved mapping (a 404).
    func importMapping(accountId: UUID) async throws -> ImportMapping? {
        do {
            return try await get("/api/imports/mappings/\(accountId.uuidString.lowercased())")
        } catch let error as APIError where error.status == 404 {
            return nil
        }
    }

    /// `PUT /api/imports/mappings/{account_id}`; replaces the account's mapping and returns it as saved.
    func saveImportMapping(accountId: UUID, _ mapping: ImportMapping) async throws -> ImportMapping {
        try await send("PUT", "/api/imports/mappings/\(accountId.uuidString.lowercased())", body: mapping)
    }

    /// `POST /api/imports/csv?account_id=`; reads the file with the account's saved mapping.
    func importCSV(accountId: UUID, data: Data, filename: String) async throws -> ImportResult {
        try await importFile("csv", accountId: accountId, data: data, filename: filename, contentType: "text/csv")
    }

    /// `POST /api/imports/ofx?account_id=`, for an OFX or QFX file.
    func importOFX(accountId: UUID, data: Data, filename: String) async throws -> ImportResult {
        try await importFile("ofx", accountId: accountId, data: data, filename: filename, contentType: "application/x-ofx")
    }

    private func importFile(
        _ format: String, accountId: UUID, data: Data, filename: String, contentType: String
    ) async throws -> ImportResult {
        try await sendMultipart(
            "POST",
            "/api/imports/\(format)",
            query: [URLQueryItem(name: "account_id", value: accountId.uuidString.lowercased())],
            parts: [.file("file", filename: filename, contentType: contentType, data: data)]
        )
    }
}
