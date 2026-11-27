import Foundation

/// Which APNs host a device token belongs to. Xcode-signed builds (Debug and Release alike) get sandbox
/// tokens; App Store and TestFlight builds get production ones.
nonisolated enum PushEnvironment: String, Codable, Sendable {
    case sandbox, production
}

/// `POST /api/devices`.
nonisolated struct DeviceRegistration: Encodable, Sendable {
    let apnsToken: String
    let environment: PushEnvironment
}

nonisolated struct RegisteredDevice: Decodable, Equatable, Sendable {
    let id: UUID
    let apnsToken: String
}
