import Network

/// Calls `onSatisfied` whenever the network path is usable, including when it comes back after being lost.
/// Driven by system path updates, not by a timer.
nonisolated final class NetworkRecoveryTrigger: Sendable {
    private let monitor = NWPathMonitor()

    init(onSatisfied: @escaping @Sendable () -> Void) {
        monitor.pathUpdateHandler = { path in
            if path.status == .satisfied { onSatisfied() }
        }
        monitor.start(queue: DispatchQueue(label: "com.luisd07.tally.network-recovery"))
    }

    deinit {
        monitor.cancel()
    }
}
