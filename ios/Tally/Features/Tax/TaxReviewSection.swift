import Observation
import SwiftUI

/// Loads, asks for and follows a year's AI review (#175). The server's worker writes it, so while it's pending the
/// model checks again every few seconds.
@Observable
final class TaxReviewModel {
    private(set) var review: TaxReview?
    private(set) var isRequesting = false
    /// Why the latest load or request failed.
    private(set) var error: String?
    @ObservationIgnored let pollInterval: Duration

    init(pollInterval: Duration = .seconds(5)) {
        self.pollInterval = pollInterval
    }

    /// Loads `year`'s review, then keeps checking while it's being written. Cancelled with the view's task.
    func follow(year: Int, api: APIClient) async {
        review = nil
        error = nil
        while !Task.isCancelled {
            do {
                review = try await api.taxReview(year: year)
            } catch is CancellationError {
                return
            } catch {
                self.error = error.localizedDescription
                return
            }
            guard review?.status.isPending == true else { return }
            try? await Task.sleep(for: pollInterval)
        }
    }

    /// Asks for a new review. Returns whether the server took it, so a failure isn't cleared by restarting `follow`.
    @discardableResult
    func request(year: Int, api: APIClient) async -> Bool {
        guard !isRequesting else { return false }
        isRequesting = true
        error = nil
        defer { isRequesting = false }
        do {
            review = try await api.requestTaxReview(year: year)
            return true
        } catch is CancellationError {
            return false
        } catch {
            self.error = error.localizedDescription
            return false
        }
    }
}

struct TaxReviewSection: View {
    let year: Int
    let model: TaxReviewModel
    /// Changes after a request, so the follow task restarts and polls.
    @Binding var requests: Int

    @Environment(APIClient.self) private var api
    @State private var confirming = false

    var body: some View {
        Section {
            if let review = model.review, review.status == .ready, let summary = review.summary {
                Label { Text(summary) } icon: { Image(systemName: "sparkles") }
                ForEach(Array(review.items.enumerated()), id: \.offset) { _, item in
                    if let transaction = item.transaction {
                        NavigationLink {
                            FindingTransactionView(transaction: transaction, onEdited: nil)
                        } label: {
                            row(item)
                        }
                    } else {
                        row(item)
                    }
                }
            } else if model.review?.status.isPending == true {
                HStack {
                    ProgressView()
                    Text("Writing the review. This takes a minute or two.")
                }
            } else if let failure = model.review?.error, model.review?.status == .failed {
                Label(failure, systemImage: "exclamationmark.triangle").foregroundStyle(.red)
            }
            if let error = model.error {
                Text(error).font(.footnote).foregroundStyle(.red)
            }
            if model.review?.status.isPending != true {
                Button(model.review == nil ? "Review \(String(year)) with AI" : "Review again", systemImage: "sparkles") {
                    confirming = true
                }
                .disabled(model.isRequesting)
            }
        } header: {
            Text("AI review for your preparer")
        } footer: {
            Text("Claude reads the year's transactions, the tax summary, mileage and income, and notes what to check. "
                + "It's added to the tax export.")
        }
        .confirmationDialog("Review \(String(year)) with AI?", isPresented: $confirming, titleVisibility: .visible) {
            Button("Review") {
                Task {
                    if await model.request(year: year, api: api) {
                        requests += 1
                    }
                }
            }
        } message: {
            Text("This sends the year's transactions (no account details) to Claude and uses about $0.50 of this "
                + "month's AI budget.")
        }
    }

    private func row(_ item: TaxReview.Item) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(item.kind.label).font(.subheadline.weight(.semibold))
            Text(item.note).font(.subheadline)
            if let transaction = item.transaction {
                Text("\(transaction.displayName) · \(transaction.amount.formatted())")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }
}
