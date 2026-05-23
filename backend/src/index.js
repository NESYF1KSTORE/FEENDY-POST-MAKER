import express from 'express';
import cors from 'cors';
import { createServer } from 'http';
import { Server as SocketIO } from 'socket.io';
import path from 'path';
import { fileURLToPath } from 'url';
import fs from 'fs';

import { config } from './config.js';
import './database.js';
import userRoutes from './routes/users.js';
import listingRoutes from './routes/listings.js';
import favoriteRoutes from './routes/favorites.js';
import chatRoutes from './routes/chats.js';
import { ChatModel } from './models/chat.js';
import { UserModel } from './models/user.js';
import { createBot, startBot } from './bot/telegram.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const app = express();
const server = createServer(app);

const io = new SocketIO(server, {
  cors: {
    origin: config.frontendUrl,
    methods: ['GET', 'POST'],
  },
});

// Middleware
app.use(cors({ origin: config.frontendUrl }));
app.use(express.json());

// Uploads directory
const uploadsDir = path.resolve(config.uploadsDir);
if (!fs.existsSync(uploadsDir)) {
  fs.mkdirSync(uploadsDir, { recursive: true });
}
app.use('/uploads', express.static(uploadsDir));

// Serve frontend build in production
const frontendDist = path.resolve(__dirname, '../../frontend/dist');
if (fs.existsSync(frontendDist)) {
  app.use(express.static(frontendDist));
}

// API Routes
app.use('/api/users', userRoutes);
app.use('/api/listings', listingRoutes);
app.use('/api/favorites', favoriteRoutes);
app.use('/api/chats', chatRoutes);

// Health check
app.get('/api/health', (_req, res) => {
  res.json({ status: 'ok', timestamp: new Date().toISOString() });
});

// Socket.IO for real-time chat
const userSockets = new Map();

io.on('connection', (socket) => {
  const telegramId = socket.handshake.query.telegramId;
  if (!telegramId) {
    socket.disconnect();
    return;
  }

  const user = UserModel.findByTelegramId(telegramId);
  if (!user) {
    socket.disconnect();
    return;
  }

  userSockets.set(user.id, socket);
  socket.userId = user.id;

  socket.on('join_chat', (chatId) => {
    const chat = ChatModel.findById(chatId);
    if (chat && (chat.buyer_id === user.id || chat.seller_id === user.id)) {
      socket.join(`chat_${chatId}`);
      ChatModel.markAsRead(chatId, user.id);
    }
  });

  socket.on('leave_chat', (chatId) => {
    socket.leave(`chat_${chatId}`);
  });

  socket.on('send_message', ({ chatId, text }) => {
    const chat = ChatModel.findById(chatId);
    if (!chat || (chat.buyer_id !== user.id && chat.seller_id !== user.id)) return;

    const message = ChatModel.sendMessage(chatId, user.id, text);
    io.to(`chat_${chatId}`).emit('new_message', message);

    const recipientId = chat.buyer_id === user.id ? chat.seller_id : chat.buyer_id;
    const recipientSocket = userSockets.get(recipientId);
    if (recipientSocket) {
      recipientSocket.emit('unread_update', { chatId, message });
    }
  });

  socket.on('typing', ({ chatId }) => {
    socket.to(`chat_${chatId}`).emit('user_typing', { chatId, userId: user.id });
  });

  socket.on('disconnect', () => {
    userSockets.delete(user.id);
  });
});

// SPA fallback
if (fs.existsSync(frontendDist)) {
  app.get('*', (_req, res) => {
    res.sendFile(path.join(frontendDist, 'index.html'));
  });
}

// Start server
server.listen(config.port, () => {
  console.log(`Server running on port ${config.port}`);
});

// Start Telegram bot
createBot();
startBot();

export { app, server, io };
