import Foundation
import Security

/// Thin wrapper over the Keychain for the access and refresh tokens.
enum Keychain {
    struct Failure: Error, LocalizedError {
        let status: OSStatus

        var errorDescription: String? {
            let message = SecCopyErrorMessageString(status, nil) as String? ?? "unknown error"
            return "Keychain error \(status): \(message)"
        }
    }

    private static func query(_ key: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: Bundle.main.bundleIdentifier ?? "com.luisd07.tally",
            kSecAttrAccount as String: key,
        ]
    }

    /// Stores `value` under `key`, replacing any existing item.
    static func save(_ value: String, for key: String) throws {
        let update = [kSecValueData as String: Data(value.utf8)]
        let status = SecItemUpdate(query(key) as CFDictionary, update as CFDictionary)
        switch status {
        case errSecSuccess:
            return
        case errSecItemNotFound:
            var item = query(key)
            item[kSecValueData as String] = Data(value.utf8)
            item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            let addStatus = SecItemAdd(item as CFDictionary, nil)
            guard addStatus == errSecSuccess else { throw Failure(status: addStatus) }
        default:
            throw Failure(status: status)
        }
    }

    /// The stored string, or nil when nothing is saved under `key`.
    static func read(_ key: String) throws -> String? {
        var item = query(key)
        item[kSecReturnData as String] = true
        item[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(item as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else { throw Failure(status: status) }
        return String(data: data, encoding: .utf8)
    }

    static func delete(_ key: String) throws {
        let status = SecItemDelete(query(key) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw Failure(status: status) }
    }
}
