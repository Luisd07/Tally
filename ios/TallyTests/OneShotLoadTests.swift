import Foundation
import Testing
@testable import Tally

/// No network: these drive `OneShotLoad` with plain closures.
@MainActor
struct OneShotLoadTests {
    @Test func failedLoadKeepsTheValueAndTheNextSuccessClearsTheError() async {
        let load = OneShotLoad<Int>()
        await load.load { 1 }
        await load.load { throw URLError(.timedOut) }
        #expect(load.value == 1)
        #expect(load.error != nil)

        await load.load { 2 }
        #expect(load.value == 2)
        #expect(load.error == nil)
    }

    @Test func cancelledLoadChangesNothing() async {
        let load = OneShotLoad<Int>()
        await load.load { throw CancellationError() }
        #expect(load.value == nil)
        #expect(load.error == nil)
    }

    @Test func onlyTheLatestLoadLands() async {
        let load = OneShotLoad<Int>()
        let (started, startedSignal) = AsyncStream.makeStream(of: Void.self)
        let (release, releaseSignal) = AsyncStream.makeStream(of: Void.self)
        let slow = Task {
            await load.load {
                startedSignal.yield()
                for await _ in release { break }
                return 1
            }
        }
        for await _ in started { break }
        #expect(load.isLoading)

        await load.load { 2 }
        releaseSignal.yield()
        await slow.value
        #expect(load.value == 2)
        #expect(!load.isLoading)
    }

    @Test func amendChangesOnlyALoadedValue() async {
        let load = OneShotLoad<[Int]>()
        load.amend { $0 + [9] }
        #expect(load.value == nil)

        await load.load { [1, 2] }
        load.amend { $0.filter { $0 != 1 } }
        #expect(load.value == [2])
    }
}
