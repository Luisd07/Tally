import Foundation

/// `GET` / `POST /api/ai/tax-review` (#175): a year's AI notes for the tax preparer.
struct TaxReview: Decodable, Equatable {
    enum Status: OpenEnum, Hashable {
        case requested, running, ready, failed, other(String)

        nonisolated static var known: [String: Status] {
            ["requested": .requested, "running": .running, "ready": .ready, "failed": .failed]
        }
        nonisolated static func unknown(_ raw: String) -> Status { .other(raw) }

        /// Still being written, so the screen checks again shortly.
        var isPending: Bool { self == .requested || self == .running }
    }

    enum Kind: OpenEnum, Hashable {
        case missedDeduction, wrongBucket, missingReceipt, mileageGap, other(String)

        nonisolated static var known: [String: Kind] {
            [
                "missed_deduction": .missedDeduction, "wrong_bucket": .wrongBucket,
                "missing_receipt": .missingReceipt, "mileage_gap": .mileageGap,
            ]
        }
        nonisolated static func unknown(_ raw: String) -> Kind { .other(raw) }

        var label: String {
            switch self {
            case .missedDeduction: "Possible missed deduction"
            case .wrongBucket: "Possibly in the wrong bucket"
            case .missingReceipt: "Missing receipt"
            case .mileageGap: "Mileage gap"
            case .other: "Worth checking"
            }
        }
    }

    struct Item: Decodable, Equatable {
        let kind: Kind
        let note: String
        let transaction: WeeklyReview.FindingTransaction?
    }

    let year: Int
    let status: Status
    let summary: String?
    let error: String?
    let updatedAt: Date
    let items: [Item]
}

struct TaxReviewBody: Encodable {
    let year: Int
}
