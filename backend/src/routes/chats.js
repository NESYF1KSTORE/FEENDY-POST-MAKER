import { Router } from 'express';
import { ChatModel } from '../models/chat.js';
import { ListingModel } from '../models/listing.js';
import { authMiddleware } from '../middleware/auth.js';

const router = Router();

router.get('/', authMiddleware, (req, res) => {
  const chats = ChatModel.getByUser(req.user.id);
  res.json(chats);
});

router.post('/', authMiddleware, (req, res) => {
  const { listingId } = req.body;
  if (!listingId) return res.status(400).json({ error: 'listingId is required' });

  const listing = ListingModel.findById(listingId);
  if (!listing) return res.status(404).json({ error: 'Listing not found' });
  if (!listing.user_id) return res.status(400).json({ error: 'Listing has no owner' });
  if (listing.user_id === req.user.id) return res.status(400).json({ error: 'Cannot chat with yourself' });

  const chat = ChatModel.getOrCreate(listingId, req.user.id, listing.user_id);
  res.json(chat);
});

router.get('/:id/messages', authMiddleware, (req, res) => {
  const chat = ChatModel.findById(req.params.id);
  if (!chat) return res.status(404).json({ error: 'Chat not found' });
  if (chat.buyer_id !== req.user.id && chat.seller_id !== req.user.id) {
    return res.status(403).json({ error: 'Forbidden' });
  }

  ChatModel.markAsRead(chat.id, req.user.id);
  const { page = 1, limit = 50 } = req.query;
  const messages = ChatModel.getMessages(chat.id, Number(page), Number(limit));
  res.json(messages);
});

router.post('/:id/messages', authMiddleware, (req, res) => {
  const chat = ChatModel.findById(req.params.id);
  if (!chat) return res.status(404).json({ error: 'Chat not found' });
  if (chat.buyer_id !== req.user.id && chat.seller_id !== req.user.id) {
    return res.status(403).json({ error: 'Forbidden' });
  }

  const { text } = req.body;
  if (!text) return res.status(400).json({ error: 'text is required' });

  const message = ChatModel.sendMessage(chat.id, req.user.id, text);
  res.status(201).json(message);
});

router.get('/unread', authMiddleware, (req, res) => {
  const count = ChatModel.getUnreadCount(req.user.id);
  res.json({ unread: count });
});

export default router;
