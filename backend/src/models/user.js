import db from '../database.js';

export const UserModel = {
  findByTelegramId(telegramId) {
    return db.prepare('SELECT * FROM users WHERE telegram_id = ?').get(String(telegramId));
  },

  findById(id) {
    return db.prepare('SELECT * FROM users WHERE id = ?').get(id);
  },

  create({ telegramId, username, firstName, lastName }) {
    const stmt = db.prepare(
      'INSERT INTO users (telegram_id, username, first_name, last_name) VALUES (?, ?, ?, ?)'
    );
    const result = stmt.run(String(telegramId), username, firstName, lastName);
    return this.findById(result.lastInsertRowid);
  },

  upsert({ telegramId, username, firstName, lastName }) {
    const existing = this.findByTelegramId(telegramId);
    if (existing) {
      db.prepare(
        'UPDATE users SET username = ?, first_name = ?, last_name = ? WHERE telegram_id = ?'
      ).run(username, firstName, lastName, String(telegramId));
      return this.findByTelegramId(telegramId);
    }
    return this.create({ telegramId, username, firstName, lastName });
  },

  updatePhone(id, phone) {
    db.prepare('UPDATE users SET phone = ? WHERE id = ?').run(phone, id);
    return this.findById(id);
  },
};
