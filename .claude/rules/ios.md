---
paths:
  - "ios/**"
---

# iOS conventions

- SwiftUI, iOS 17+, Observation (`@Observable`), URLSession with async/await. No third-party packages.
- No financial data is stored on the phone. Screens load live from the API; only Keychain tokens and the outbox (unsent captures, with complete file protection) persist. The tax export zip and the transactions CSV may exist only as a protected temp file in `tmp/Export/<kind>/` (one directory per export kind) while it is being shared (deleted when the share completes and before each export) and is swept at launch. UserDefaults may hold non-financial preferences (server URL, last-used card id and bucket), cleared on sign-out where they are user-specific.
- Captures carry a phone-generated UUID so retries never duplicate rows on the server.
- `Models/` mirrors the API's JSON. The API uses snake_case; `APIClient` decodes with `.convertFromSnakeCase`.
- Layout: `App/` (entry, tab bar), `Features/<Tab>/`, `Models/`, `Services/`.
- The Xcode project is `ios/Tally.xcodeproj` with folder-synchronized groups: new files under `ios/Tally/` or `ios/TallyTests/` need no project edit.
- Swift 6 with default MainActor isolation.
- Tests use Swift Testing in `ios/TallyTests/`, with JSON fixtures under `ios/TallyTests/Fixtures/`.
- Money is the `Money` type (JSON string both ways, always two decimals); date-only fields are `CalendarDate`; server string enums adopt `OpenEnum` for an `.other(String)` fallback.
- Endpoints are typed `APIClient` extensions in `Services/Endpoints.swift`; views and models never build `/api/...` paths.
- Network tests use `StubURLProtocol` (one global handler): add them to the `.serialized` `APIClientTests` suite, not a new parallel suite.
- Dev sign-in and the plain-HTTP exception are DEBUG-only. Keep them out of release builds.
