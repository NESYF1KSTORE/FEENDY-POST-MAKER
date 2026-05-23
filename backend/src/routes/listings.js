import { Router } from 'express';
import { ListingModel } from '../models/listing.js';
import { FavoriteModel } from '../models/favorite.js';
import { authMiddleware, optionalAuth } from '../middleware/auth.js';

const router = Router();

router.get('/', optionalAuth, (req, res) => {
  const result = ListingModel.search(req.query);

  if (req.user) {
    result.items = result.items.map((item) => ({
      ...item,
      isFavorite: FavoriteModel.isFavorite(req.user.id, item.id),
    }));
  }

  res.json(result);
});

router.get('/map', optionalAuth, (req, res) => {
  const listings = ListingModel.getMapListings(req.query);
  res.json(listings);
});

router.get('/my', authMiddleware, (req, res) => {
  const { page = 1, limit = 20 } = req.query;
  const result = ListingModel.getByUser(req.user.id, Number(page), Number(limit));
  res.json(result);
});

router.get('/:id', optionalAuth, (req, res) => {
  const listing = ListingModel.findById(req.params.id);
  if (!listing) {
    return res.status(404).json({ error: 'Listing not found' });
  }

  ListingModel.incrementViews(listing.id);
  const parsed = ListingModel._parseJson({ ...listing });

  if (req.user) {
    parsed.isFavorite = FavoriteModel.isFavorite(req.user.id, parsed.id);
  }

  res.json(parsed);
});

router.post('/', authMiddleware, (req, res) => {
  const data = { ...req.body, userId: req.user.id, source: 'user' };
  if (!data.title || !data.price || !data.address) {
    return res.status(400).json({ error: 'title, price, and address are required' });
  }
  const listing = ListingModel.create(data);
  res.status(201).json(ListingModel._parseJson(listing));
});

router.patch('/:id', authMiddleware, (req, res) => {
  const listing = ListingModel.findById(req.params.id);
  if (!listing) return res.status(404).json({ error: 'Listing not found' });
  if (listing.user_id !== req.user.id) return res.status(403).json({ error: 'Forbidden' });

  const updated = ListingModel.update(req.params.id, req.body);
  res.json(ListingModel._parseJson(updated));
});

router.delete('/:id', authMiddleware, (req, res) => {
  const listing = ListingModel.findById(req.params.id);
  if (!listing) return res.status(404).json({ error: 'Listing not found' });
  if (listing.user_id !== req.user.id) return res.status(403).json({ error: 'Forbidden' });

  ListingModel.delete(req.params.id);
  res.json({ success: true });
});

export default router;
