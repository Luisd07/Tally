import Foundation
import Testing
@testable import Tally

@MainActor
struct ModelTests {
    private struct Wrapper: Codable { let value: Money }

    private func roundTrip(_ json: String) throws -> String {
        let wrapper = try JSONDecoder().decode(Wrapper.self, from: Data(#"{"value":"\#(json)"}"#.utf8))
        let data = try JSONEncoder().encode(wrapper)
        return try JSONDecoder().decode([String: String].self, from: data)["value"] ?? ""
    }

    @Test func moneyRoundTripsExactDigits() throws {
        #expect(try roundTrip("156.60") == "156.60")
        #expect(try roundTrip("-85.00") == "-85.00")
        #expect(try roundTrip("0.05") == "0.05")
        #expect(try roundTrip("1234567.89") == "1234567.89")
    }

    @Test func moneyEncodesWholeNumbersWithTwoDecimals() throws {
        #expect(try roundTrip("220") == "220.00")
    }

    @Test func moneyEncodesAsStringRegardlessOfEncoderSettings() throws {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let json = String(decoding: try encoder.encode(Wrapper(value: Money(Decimal(string: "156.60")!))), as: UTF8.self)
        #expect(json == #"{"value":"156.60"}"#)
    }

    @Test func moneyRejectsInvalidStrings() {
        #expect(throws: DecodingError.self) {
            try JSONDecoder().decode(Wrapper.self, from: Data(#"{"value":"abc"}"#.utf8))
        }
    }

    @Test func moneyArithmeticAndOrdering() {
        let a = Money(Decimal(string: "10.10")!)
        let b = Money(Decimal(string: "0.20")!)
        #expect(a + b == Money(Decimal(string: "10.30")!))
        #expect(a - b == Money(Decimal(string: "9.90")!))
        #expect(b < a)
        #expect(Money(Decimal(string: "-1")!) < b)
    }

    @Test func moneyFormatsAsUSD() {
        #expect(Money(Decimal(string: "1234.5")!).formatted().contains("1,234.50"))
    }

    @Test func moneyUserInputAcceptsPlainAndGroupedAmounts() {
        #expect(Money(userInput: "1500") == Money(Decimal(string: "1500")!))
        #expect(Money(userInput: "1500.5") == Money(Decimal(string: "1500.50")!))
        #expect(Money(userInput: "0") == Money(0))
        #expect(Money(userInput: " 12.34 ") == Money(Decimal(string: "12.34")!))
        #expect(Money(userInput: "1,000") == Money(Decimal(string: "1000.00")!))
        #expect(Money(userInput: "12,345,678.90") == Money(Decimal(string: "12345678.90")!))
        #expect(Money(userInput: "5.") == Money(Decimal(string: "5")!))
        #expect(Money(userInput: ".5") == Money(Decimal(string: "0.50")!))
    }

    @Test(arguments: ["", "   ", "12abc", "1.2.3", "-5", "1.234", "1,00", "10,00.5", ",100", "1,000,", "$5", ".", "1 000"])
    func moneyUserInputRejects(_ text: String) {
        #expect(Money(userInput: text) == nil)
    }

    @Test func moneyInputTextReadsBack() {
        let amount = Money(Decimal(string: "1234.5")!)
        #expect(amount.inputText == "1234.50")
        #expect(Money(userInput: amount.inputText) == amount)
    }

    @Test func moneyNegatesAndRoundsToCentsHalfToEven() {
        func money(_ text: String) -> Money { Money(Decimal(string: text)!) }
        #expect(money("-15.49").negated == money("15.49"))
        #expect(money("0").negated == money("0"))
        #expect(money("0.125").roundedToCents == money("0.12"))
        #expect(money("0.135").roundedToCents == money("0.14"))
        #expect(money("-0.125").roundedToCents == money("-0.12"))
        #expect(money("-185.88").roundedToCents == money("-185.88"))
        #expect(Money(Decimal(string: "-185.88")! / 12).roundedToCents == money("-15.49"))
    }

    @Test func pickerOptionsAddOnlyAnUnknownCurrentValue() {
        #expect(Bucket.pickerOptions(including: .business) == Bucket.allKnown)
        #expect(Bucket.pickerOptions(including: .other("charity")) == Bucket.allKnown + [.other("charity")])
        #expect(IncomeType.pickerOptions(including: .unrecognized("tips")).last == .unrecognized("tips"))
        #expect(Cadence.known["quarterly"] == .quarterly)
        #expect(SubscriptionKind.known.count == SubscriptionKind.allKnown.count)
    }

    @Test func unknownBucketAndBillKindFallBack() throws {
        struct Holder: Decodable { let bucket: Bucket; let kind: BillKind }
        let known = try JSONDecoder().decode(Holder.self, from: Data(#"{"bucket":"business","kind":"card"}"#.utf8))
        #expect(known.bucket == .business)
        #expect(known.kind == .card)
        let unknown = try JSONDecoder().decode(Holder.self, from: Data(#"{"bucket":"charity","kind":"loan"}"#.utf8))
        #expect(unknown.bucket == .other("charity"))
        #expect(unknown.kind == .other("loan"))
    }
}
