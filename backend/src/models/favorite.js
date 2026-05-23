import db from '../database.js';

export const FavoriteModel = {
  getByUser(userId, page = 1, limit = 20) {
    const offset = (page - 1) * limit;
    const { total } = db.prepare(
      'SELECT COUNT(*) as total FROM favorites WHERE user_id = ?'
    ).get(userId);

    const items = db.prepare(`
      SELECT l.*, f.created_at as favorited_at
      FROM favorites f
      JOIN listings l ON f.listing_id = l.id
      WHERE f.user_id = ?
      ORDER BY f.created_at DESC
      LIMIT ? OFFSET ?
    `).all(userId, limit, offset);

    return {
      items: items.map((item) => {
        try { item.images = JSON.parse(item.images || '[]'); } catch { item.images = []; }
        try { item.features = JSON.parse(item.features || '[]'); } catch { item.features = []; }
        return item;
      }),
      total,
      page,
      pages: Math.ceil(total / limit),
    };
  },

  add(userId, listingId) {
    try {
      db.prepare('INSERT INTO favorites (user_id, listing_id) VALUES (?, ?)').run(userId, listingId);
      return true;
    } catch {
      return false;
    }
  },

  remove(userId, listingId) {
    db.prepare('DELETE FROM favorites WHERE user_id = ? AND listing_id = ?').run(userId, listingId);
  },

  isFavorite(userId, listingId) {
    const row = db.prepare('SELECT 1 FROM favorites WHERE user_id = ? AND listing_id = ?').get(userId, listingId);
    return !!row;
  },

  count(userId) {
    const { total } = db.prepare('SELECT COUNT(*) as total FROM favorites WHERE user_id = ?').get(userId);
    return total;
  },
};
