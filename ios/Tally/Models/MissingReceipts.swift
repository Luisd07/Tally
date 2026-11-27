import Foundation

/// The buckets `GET /api/tax/missing-receipts` accepts; the server rejects any other with 422.
enum ReceiptBucket: CaseIterable, Identifiable {
    case business, reimbursable

    var id: Self { self }

    /// The wire string and the label are `Bucket`'s.
    var bucket: Bucket {
        switch self {
        case .business: .business
        case .reimbursable: .reimbursable
        }
    }

    var label: String { bucket.label }
}

/// Mirrors `MissingReceiptsPage` from `GET /api/tax/missing-receipts`; `count` is every match, not just this page.
struct MissingReceiptPage: Decodable {
    let items: [Transaction]
    let nextCursor: String?
    let count: Int
}
