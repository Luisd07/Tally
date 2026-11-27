import PhotosUI
import SwiftUI

struct AddView: View {
    @Environment(APIClient.self) private var api
    @Environment(Outbox.self) private var outbox
    @State private var model = AddModel()
    @State private var photo: PhotosPickerItem?
    @State private var showsScanner = false
    /// Whether the server has AI assist on, so the quick-capture field shows (#171).
    @State private var aiEnabled = false
    @State private var showsMoreDetails = false
    @State private var savedCount = 0
    @State private var showsSaved = false
    @State private var isCreatingCategory = false

    var body: some View {
        NavigationStack {
            ScrollView { form }
                .scrollDismissesKeyboard(.interactively)
                .safeAreaInset(edge: .bottom) { saveBar }
                .navigationTitle("Add")
                .navigationBarTitleDisplayMode(.inline)
                .task { await model.load(api: api) }
                .task { aiEnabled = (try? await api.aiUsage())?.enabled ?? false }
                .task(id: savedCount) { await flashSaved() }
                .sensoryFeedback(.success, trigger: savedCount)
                .onChange(of: photo) { _, picked in
                    guard let picked else { return }
                    photo = nil
                    Task { await attachPhoto(picked) }
                }
                .receiptScanner(isPresented: $showsScanner) { pages in
                    Task { await model.attachReceipt(scannedPages: pages) }
                } onFailure: {
                    model.receiptError = "The scan didn't finish. Try again."
                }
                .sheet(isPresented: $isCreatingCategory) {
                    NewCategorySheet(kind: model.direction == .expense ? .expense : .income) { model.addCategory($0) }
                }
        }
    }

    private var form: some View {
        VStack(spacing: 16) {
            if aiEnabled { quickCapture }
            directionPicker
            amountDisplay
            AddKeypad(model: model)
            if let loadError = model.loadError {
                loadErrorView(loadError)
            }
            fields
            receiptSection
            moreDetails
        }
        .padding()
    }

    private func flashSaved() async {
        guard savedCount > 0 else { return }
        showsSaved = true
        try? await Task.sleep(for: .seconds(2.5))
        showsSaved = false
    }

    // MARK: Quick capture

    private var quickCapture: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                TextField("Describe it: lunch Chipotle 18.40 business", text: $model.note)
                    .textFieldStyle(.roundedBorder)
                    .submitLabel(.go)
                    .onSubmit { Task { await model.readNote(api: api) } }
                if model.isReadingNote {
                    ProgressView()
                } else {
                    Button("Fill in", systemImage: "sparkles") { Task { await model.readNote(api: api) } }
                        .labelStyle(.iconOnly)
                        .disabled(model.note.trimmedOrNil == nil)
                }
            }
            if let noteError = model.noteError {
                Text(noteError).font(.footnote).foregroundStyle(.red)
            }
        }
    }

    // MARK: Amount

    private var directionPicker: some View {
        Picker("Type", selection: $model.direction) {
            Text("Expense").tag(AddModel.Direction.expense)
            Text("Money in").tag(AddModel.Direction.moneyIn)
        }
        .pickerStyle(.segmented)
    }

    private var amountDisplay: some View {
        HStack {
            Text(amountString)
                .font(.system(size: 52, weight: .semibold, design: .rounded))
                .monospacedDigit()
                .minimumScaleFactor(0.5)
                .lineLimit(1)
                .foregroundStyle(model.amountText.isEmpty ? .secondary : .primary)
                .frame(maxWidth: .infinity, alignment: .trailing)
                .accessibilityLabel("Amount \(amountString)")
            Button("Clear") { model.clear() }
                .disabled(model.amountText.isEmpty)
        }
    }

    private var amountString: String {
        let sign = model.direction == .expense ? "-" : "+"
        return sign + "$" + (model.amountText.isEmpty ? "0" : model.amountText)
    }

    // MARK: Fields

    private var fields: some View {
        VStack(spacing: 10) {
            categoryRow
            cardRow
            bucketRow
            if !model.events.isEmpty {
                eventRow
            }
            dateRow
        }
    }

    private var categoryRow: some View {
        MenuRow(title: "Category", value: model.categories.first { $0.id == model.categoryId }?.name ?? "None") {
            Picker("Category", selection: $model.categoryId) {
                Text("None").tag(UUID?.none)
                ForEach(model.availableCategories) { category in
                    Text(category.name).tag(UUID?.some(category.id))
                }
            }
            .pickerStyle(.inline)
            Button("New category…", systemImage: "plus") { isCreatingCategory = true }
        }
    }

    private var cardRow: some View {
        let card = Binding(get: { model.accountId }, set: { if let id = $0 { model.selectAccount(id) } })
        return MenuRow(title: "Card", value: model.activeAccounts.first { $0.id == model.accountId }.map(accountLabel) ?? "Choose") {
            Picker("Card", selection: card) {
                ForEach(model.activeAccounts) { account in
                    Text(accountLabel(account)).tag(UUID?.some(account.id))
                }
            }
            .pickerStyle(.inline)
        }
    }

    private var bucketRow: some View {
        MenuRow(title: "Bucket", value: model.bucket.label) {
            Picker("Bucket", selection: $model.bucket) {
                ForEach(Bucket.allKnown, id: \.self) { bucket in
                    Text(bucket.label).tag(bucket)
                }
            }
            .pickerStyle(.inline)
        }
    }

    private var eventRow: some View {
        MenuRow(title: "Event", value: model.events.first { $0.id == model.eventId }?.name ?? "None") {
            Picker("Event", selection: $model.eventId) {
                Text("None").tag(UUID?.none)
                ForEach(model.events) { event in
                    Text(event.name).tag(UUID?.some(event.id))
                }
            }
            .pickerStyle(.inline)
        }
    }

    private var dateRow: some View {
        DatePicker("Date", selection: $model.dateValue, displayedComponents: .date)
            .padding(.horizontal)
            .frame(minHeight: 48)
            .background(.fill.tertiary, in: RoundedRectangle(cornerRadius: 12))
    }

    private func accountLabel(_ account: Account) -> String {
        account.last4.map { "\(account.name) ••\($0)" } ?? account.name
    }

    private func loadErrorView(_ message: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(message, systemImage: "exclamationmark.triangle")
                .foregroundStyle(.red)
            Button("Retry") { Task { await model.load(api: api) } }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: Receipt

    private var receiptSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            if model.receiptJPEG != nil {
                attachedReceipt
            } else {
                let canScan = ReceiptScanner.isSupported
                HStack {
                    if canScan {
                        Button { showsScanner = true } label: {
                            Label("Scan receipt", systemImage: "doc.viewfinder")
                                .frame(maxWidth: .infinity, minHeight: 44)
                        }
                    }
                    PhotosPicker(selection: $photo, matching: .images) {
                        Label(canScan ? "Library" : "Add receipt photo", systemImage: "photo")
                            .frame(maxWidth: .infinity, minHeight: 44)
                    }
                }
                .buttonStyle(.bordered)
            }
            if let receiptError = model.receiptError {
                Text(receiptError).font(.footnote).foregroundStyle(.red)
            }
        }
    }

    private var attachedReceipt: some View {
        HStack {
            if let thumbnail = model.receiptThumbnail {
                Image(uiImage: thumbnail)
                    .resizable()
                    .scaledToFill()
                    .frame(width: 44, height: 44)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
            }
            Label("Receipt attached", systemImage: "checkmark.circle.fill")
            Spacer()
            Button("Remove", role: .destructive) { model.removeReceipt() }
        }
    }

    private func attachPhoto(_ picked: PhotosPickerItem) async {
        guard let data = try? await picked.loadTransferable(type: Data.self) else {
            model.receiptError = "Couldn't read the photo."
            return
        }
        await model.attachReceipt(imageData: data)
    }

    // MARK: More details

    private var moreDetails: some View {
        DisclosureGroup("More details", isExpanded: $showsMoreDetails) {
            VStack(spacing: 10) {
                TextField("Description", text: $model.description)
                TextField("Merchant", text: $model.merchant)
                TextField("Notes", text: $model.notes, axis: .vertical)
                    .lineLimit(1...4)
            }
            .textFieldStyle(.roundedBorder)
            .padding(.top, 8)
        }
    }

    // MARK: Save

    private var saveBar: some View {
        VStack(spacing: 8) {
            if let problem = outbox.storageProblem {
                statusLabel(problem, systemImage: "exclamationmark.triangle")
            }
            if let saveError = model.saveError {
                statusLabel(saveError, systemImage: "xmark.octagon")
            }
            if showsSaved {
                Label("Saved — sending", systemImage: "checkmark.circle.fill")
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(.green)
            }
            saveButton
        }
        .padding(.horizontal)
        .padding(.vertical, 10)
        .background(.bar)
    }

    private func statusLabel(_ message: String, systemImage: String) -> some View {
        Label(message, systemImage: systemImage)
            .font(.footnote)
            .foregroundStyle(.red)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var saveButton: some View {
        Button(action: save) {
            Text("Save")
                .font(.title3.weight(.semibold))
                .frame(maxWidth: .infinity, minHeight: 52)
        }
        .buttonStyle(.borderedProminent)
        .disabled(!model.canSave)
    }

    private func save() {
        if model.save(outbox: outbox) {
            showsMoreDetails = false
            savedCount += 1
        }
    }
}
