import { UserModel } from '../models/user.js';

export function authMiddleware(req, res, next) {
  const telegramId = req.headers['x-telegram-user-id'];
  if (!telegramId) {
    return res.status(401).json({ error: 'Telegram user ID required' });
  }

  const user = UserModel.findByTelegramId(telegramId);
  if (!user) {
    return res.status(401).json({ error: 'User not found. Please authenticate first.' });
  }

  req.user = user;
  next();
}

export function optionalAuth(req, _res, next) {
  const telegramId = req.headers['x-telegram-user-id'];
  if (telegramId) {
    req.user = UserModel.findByTelegramId(telegramId);
  }
  next();
}
