import Foundation

/// Body for `POST /api/ai/chat` (#174).
struct ChatBody: Encodable {
    let conversationId: UUID
    let message: String
    /// This build shows proposed changes (#223), so the server may offer them.
    let actionsSupported = true
}

/// `POST /api/ai/chat`'s answer: the reply and what it rests on, in plain words.
struct ChatReply: Decodable, Equatable {
    let conversationId: UUID
    let reply: String
    let lookedUp: [String]
    let actions: [AIAction]

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        conversationId = try container.decode(UUID.self, forKey: .conversationId)
        reply = try container.decode(String.self, forKey: .reply)
        lookedUp = try container.decode([String].self, forKey: .lookedUp)
        actions = try container.decodeIfPresent([AIAction].self, forKey: .actions) ?? []
    }

    private enum CodingKeys: String, CodingKey {
        case conversationId, reply, lookedUp, actions
    }
}
