import db from '../database.js';

export const ChatModel = {
  findById(id) {
    return db.prepare('SELECT * FROM chats WHERE id = ?').get(id);
  },

  getOrCreate(listingId, buyerId, sellerId) {
    const existing = db.prepare(
      'SELECT * FROM chats WHERE listing_id = ? AND buyer_id = ?'
    ).get(listingId, buyerId);

    if (existing) return existing;

    const result = db.prepare(
      'INSERT INTO chats (listing_id, buyer_id, seller_id) VALUES (?, ?, ?)'
    ).run(listingId, buyerId, sellerId);

    return this.findById(result.lastInsertRowid);
  },

  getByUser(userId) {
    return db.prepare(`
      SELECT c.*,
        l.title as listing_title, l.price as listing_price, l.images as listing_images,
        l.address as listing_address,
        u_buyer.first_name as buyer_name, u_buyer.username as buyer_username,
        u_seller.first_name as seller_name, u_seller.username as seller_username,
        (SELECT text FROM messages WHERE chat_id = c.id ORDER BY created_at DESC LIMIT 1) as last_message,
        (SELECT created_at FROM messages WHERE chat_id = c.id ORDER BY created_at DESC LIMIT 1) as last_message_at,
        (SELECT COUNT(*) FROM messages WHERE chat_id = c.id AND sender_id != ? AND is_read = 0) as unread_count
      FROM chats c
      JOIN listings l ON c.listing_id = l.id
      JOIN users u_buyer ON c.buyer_id = u_buyer.id
      JOIN users u_seller ON c.seller_id = u_seller.id
      WHERE c.buyer_id = ? OR c.seller_id = ?
      ORDER BY last_message_at DESC NULLS LAST
    `).all(userId, userId, userId).map((chat) => {
      try { chat.listing_images = JSON.parse(chat.listing_images || '[]'); } catch { chat.listing_images = []; }
      return chat;
    });
  },

  getMessages(chatId, page = 1, limit = 50) {
    const offset = (page - 1) * limit;
    const { total } = db.prepare('SELECT COUNT(*) as total FROM messages WHERE chat_id = ?').get(chatId);
    const items = db.prepare(`
      SELECT m.*, u.first_name as sender_name, u.username as sender_username
      FROM messages m
      JOIN users u ON m.sender_id = u.id
      WHERE m.chat_id = ?
      ORDER BY m.created_at ASC
      LIMIT ? OFFSET ?
    `).all(chatId, limit, offset);

    return { items, total, page, pages: Math.ceil(total / limit) };
  },

  sendMessage(chatId, senderId, text) {
    const result = db.prepare(
      'INSERT INTO messages (chat_id, sender_id, text) VALUES (?, ?, ?)'
    ).run(chatId, senderId, text);

    return db.prepare(`
      SELECT m.*, u.first_name as sender_name, u.username as sender_username
      FROM messages m JOIN users u ON m.sender_id = u.id
      WHERE m.id = ?
    `).get(result.lastInsertRowid);
  },

  markAsRead(chatId, userId) {
    db.prepare(
      'UPDATE messages SET is_read = 1 WHERE chat_id = ? AND sender_id != ? AND is_read = 0'
    ).run(chatId, userId);
  },

  getUnreadCount(userId) {
    const { total } = db.prepare(`
      SELECT COUNT(*) as total FROM messages m
      JOIN chats c ON m.chat_id = c.id
      WHERE m.sender_id != ? AND m.is_read = 0 AND (c.buyer_id = ? OR c.seller_id = ?)
    `).get(userId, userId, userId);
    return total;
  },
};
