import Foundation

/// One child row of `POST /api/transactions/{id}/split`. `amount` carries the parent's sign. A nil `categoryId`
/// or `bucket` is left out of the body, so the child inherits the parent's value.
struct SplitPart: Encodable, Equatable {
    var amount: Money
    var categoryId: UUID?
    var bucket: Bucket?
}

/// Body for `POST /api/transactions/{id}/split`: 2..20 parts that sum to the parent's amount.
struct SplitRequest: Encodable {
    let parts: [SplitPart]
}

/// Mirrors `TransactionSplitResult`: the parent, now split, and its children.
struct SplitResult: Decodable {
    let parent: Transaction
    let children: [Transaction]
}
