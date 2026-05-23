import 'dotenv/config';

export const config = {
  port: process.env.PORT || 3000,
  telegramBotToken: process.env.TELEGRAM_BOT_TOKEN || '',
  frontendUrl: process.env.FRONTEND_URL || 'http://localhost:5173',
  miniAppUrl: process.env.MINI_APP_URL || 'http://localhost:5173',
  dbPath: process.env.DB_PATH || './data/cian.db',
  uploadsDir: process.env.UPLOADS_DIR || './uploads',
};
