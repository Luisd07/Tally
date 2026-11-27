import Foundation

/// A sync run's outcome; unknown server values decode to `.other`.
enum SyncRunStatus: OpenEnum, Hashable {
    case running, ok, partial, failed, other(String)

    nonisolated static var known: [String: SyncRunStatus] {
        ["running": .running, "ok": .ok, "partial": .partial, "failed": .failed]
    }
    nonisolated static func unknown(_ raw: String) -> SyncRunStatus { .other(raw) }
}

/// Mirrors `SyncRunOut`: one SimpleFIN sync, nightly or "Sync now".
struct SyncRun: Decodable, Identifiable, Hashable {
    /// One sanitized problem the run reported, e.g. a bank SimpleFIN couldn't reach.
    struct Problem: Decodable, Hashable {
        let msg: String?
    }

    let id: UUID
    let startedAt: Date
    let finishedAt: Date?
    let status: SyncRunStatus
    let newCount: Int
    let updatedCount: Int
    let errors: [Problem]
}

/// `GET /api/sync/runs`, newest first.
struct SyncRunPage: Decodable {
    let items: [SyncRun]
    let nextCursor: String?
}

/// `POST /api/sync/simplefin/claim`: when the connection was made and the SimpleFIN host it points at.
struct SimplefinClaim: Decodable {
    let claimedAt: Date
    let host: String
}

/// `POST /api/sync/simplefin/claim` body.
struct SimplefinClaimBody: Encodable {
    let setupToken: String
}
