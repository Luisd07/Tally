import Foundation

/// What happened at sign-in; unknown server values decode to `.other`.
enum AuthEventKind: OpenEnum, Hashable {
    case enroll, enrollFailed, signIn, signInFailed, refresh, refreshReuse, refreshFailed, signOut
    case passkeysReplaced, attest, attestFailed, other(String)

    nonisolated static var known: [String: AuthEventKind] {
        [
            "enroll": .enroll, "enroll_failed": .enrollFailed, "sign_in": .signIn, "sign_in_failed": .signInFailed,
            "refresh": .refresh, "refresh_reuse": .refreshReuse, "refresh_failed": .refreshFailed,
            "sign_out": .signOut, "passkeys_replaced": .passkeysReplaced, "attest": .attest,
            "attest_failed": .attestFailed,
        ]
    }
    nonisolated static func unknown(_ raw: String) -> AuthEventKind { .other(raw) }

    /// A refused or suspicious attempt, shown in the attention color.
    var isFailure: Bool {
        switch self {
        case .enrollFailed, .signInFailed, .refreshReuse, .refreshFailed, .attestFailed: true
        default: false
        }
    }

    var label: String {
        switch self {
        case .enroll: "Passkey added"
        case .enrollFailed: "Passkey setup refused"
        case .signIn: "Signed in"
        case .signInFailed: "Sign-in refused"
        case .refresh: "Session renewed"
        case .refreshReuse: "Old session token reused"
        case .refreshFailed: "Session renewal refused"
        case .signOut: "Signed out"
        case .passkeysReplaced: "Passkeys replaced"
        case .attest: "Device attested"
        case .attestFailed: "Device attestation refused"
        case .other(let raw): raw
        }
    }

    var symbol: String {
        switch self {
        case .enroll, .passkeysReplaced: "person.badge.key"
        case .signIn: "arrow.right.circle"
        case .signOut: "arrow.left.circle"
        case .refresh: "arrow.triangle.2.circlepath"
        case .attest: "checkmark.shield"
        default: isFailure ? "exclamationmark.triangle" : "circle"
        }
    }
}

/// Mirrors `AuthEventOut` from `GET /api/auth/events`.
struct AuthEvent: Decodable, Identifiable, Hashable {
    let id: UUID
    let kind: AuthEventKind
    let ip: String?
    let userAgent: String?
    let createdAt: Date
}

/// `GET /api/auth/events`, newest first.
struct AuthEventPage: Decodable {
    let items: [AuthEvent]
    let nextCursor: String?
}
