import Foundation
import Observation

/// One file shared out of the app: downloads it, keeps it in a protected temp file while the share sheet is up
/// and deletes it when the share completes.
@Observable
final class ExportModel {
    enum State: Equatable {
        case idle
        case exporting
        /// The file is on disk and the share sheet is up.
        case ready(URL)
        case failed(String)
    }

    private(set) var state = State.idle
    private let directory: URL

    init(kind: ExportFile.Kind, root: URL = ExportFile.root) {
        directory = ExportFile.directory(for: kind, in: root)
    }

    var isExporting: Bool { state == .exporting }

    var readyURL: URL? {
        guard case .ready(let url) = state else { return nil }
        return url
    }

    var failureMessage: String? {
        guard case .failed(let message) = state else { return nil }
        return message
    }

    /// Fetches the file and writes it as `name` to a protected temp file (replacing any earlier one). Does nothing
    /// while another export runs. A cancelled export returns silently to idle; any other error is shown.
    func export(named name: String, fetch: () async throws -> Data) async {
        guard !isExporting else { return }
        state = .exporting
        do {
            let data = try await fetch()
            state = .ready(try ExportFile.write(data, named: name, in: directory))
        } catch is CancellationError {
            state = .idle
        } catch {
            state = .failed(error.localizedDescription)
        }
    }

    /// The share finished or was cancelled (the activity's completion handler): delete the file.
    func finish() {
        ExportFile.removeLeftovers(in: directory)
        state = .idle
    }

    /// The sheet went away. The file stays until `finish`, the next export or the next launch, because an
    /// activity may still be reading it.
    func sheetDismissed() {
        guard case .ready = state else { return }
        state = .idle
    }

    /// The failure alert was acknowledged.
    func clearFailure() {
        guard case .failed = state else { return }
        state = .idle
    }
}
