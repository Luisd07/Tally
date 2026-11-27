/// A rule a sheet's fields break, with the reason shown beside Save.
protocol FormRule: Error {
    var message: String { get }
}

extension Result where Failure: FormRule {
    /// Why the fields are invalid, or nil when they are valid.
    var invalidMessage: String? {
        guard case .failure(let rule) = self else { return nil }
        return rule.message
    }
}
