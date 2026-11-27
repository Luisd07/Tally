import Foundation

/// The card and bucket the last saved expense used, so the next one starts there.
/// Only the card id and the bucket string are kept, and `Session.signOut` clears them.
struct CapturePreferences {
    private static let accountKey = "add_last_account_id"
    private static let bucketKey = "add_last_bucket"

    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    var accountId: UUID? {
        defaults.string(forKey: Self.accountKey).flatMap { UUID(uuidString: $0) }
    }

    var bucket: Bucket? {
        defaults.string(forKey: Self.bucketKey).flatMap { Bucket.known[$0] }
    }

    func record(accountId: UUID, bucket: Bucket) {
        defaults.set(accountId.uuidString.lowercased(), forKey: Self.accountKey)
        defaults.set(bucket.rawValue, forKey: Self.bucketKey)
    }

    func clear() {
        defaults.removeObject(forKey: Self.accountKey)
        defaults.removeObject(forKey: Self.bucketKey)
    }
}
