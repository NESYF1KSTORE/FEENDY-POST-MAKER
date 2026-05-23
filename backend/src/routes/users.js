import { Router } from 'express';
import { UserModel } from '../models/user.js';
import { authMiddleware } from '../middleware/auth.js';

const router = Router();

router.post('/auth', (req, res) => {
  const { telegramId, username, firstName, lastName } = req.body;
  if (!telegramId) {
    return res.status(400).json({ error: 'telegramId is required' });
  }
  const user = UserModel.upsert({ telegramId, username, firstName, lastName });
  res.json(user);
});

router.get('/me', authMiddleware, (req, res) => {
  res.json(req.user);
});

router.patch('/me', authMiddleware, (req, res) => {
  const { phone } = req.body;
  if (phone) {
    const updated = UserModel.updatePhone(req.user.id, phone);
    return res.json(updated);
  }
  res.json(req.user);
});

export default router;
