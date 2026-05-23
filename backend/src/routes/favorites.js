import { Router } from 'express';
import { FavoriteModel } from '../models/favorite.js';
import { authMiddleware } from '../middleware/auth.js';

const router = Router();

router.get('/', authMiddleware, (req, res) => {
  const { page = 1, limit = 20 } = req.query;
  const result = FavoriteModel.getByUser(req.user.id, Number(page), Number(limit));
  res.json(result);
});

router.post('/:listingId', authMiddleware, (req, res) => {
  const added = FavoriteModel.add(req.user.id, Number(req.params.listingId));
  if (!added) {
    return res.status(409).json({ error: 'Already in favorites' });
  }
  res.json({ success: true, count: FavoriteModel.count(req.user.id) });
});

router.delete('/:listingId', authMiddleware, (req, res) => {
  FavoriteModel.remove(req.user.id, Number(req.params.listingId));
  res.json({ success: true, count: FavoriteModel.count(req.user.id) });
});

export default router;
