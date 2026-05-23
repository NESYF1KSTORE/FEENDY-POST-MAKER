import { Bot, InlineKeyboard } from 'grammy';
import { config } from '../config.js';

let bot = null;

export function createBot() {
  if (!config.telegramBotToken) {
    console.warn('TELEGRAM_BOT_TOKEN not set, bot disabled');
    return null;
  }

  bot = new Bot(config.telegramBotToken);

  bot.command('start', async (ctx) => {
    const keyboard = new InlineKeyboard()
      .webApp('🏠 Открыть Циан', config.miniAppUrl);

    await ctx.reply(
      '🏠 *Добро пожаловать в Циан Mini App!*\n\n' +
      'Здесь вы можете:\n' +
      '🔍 Искать квартиры и дома\n' +
      '🗺 Смотреть объекты на карте\n' +
      '❤️ Сохранять в избранное\n' +
      '💬 Общаться с продавцами\n' +
      '📝 Подавать объявления\n\n' +
      'Нажмите кнопку ниже, чтобы начать:',
      {
        parse_mode: 'Markdown',
        reply_markup: keyboard,
      }
    );
  });

  bot.command('search', async (ctx) => {
    const keyboard = new InlineKeyboard()
      .webApp('🔍 Поиск', `${config.miniAppUrl}/search`);

    await ctx.reply('Нажмите, чтобы начать поиск:', { reply_markup: keyboard });
  });

  bot.command('favorites', async (ctx) => {
    const keyboard = new InlineKeyboard()
      .webApp('❤️ Избранное', `${config.miniAppUrl}/favorites`);

    await ctx.reply('Ваши избранные объекты:', { reply_markup: keyboard });
  });

  bot.command('create', async (ctx) => {
    const keyboard = new InlineKeyboard()
      .webApp('📝 Подать объявление', `${config.miniAppUrl}/create`);

    await ctx.reply('Подать новое объявление:', { reply_markup: keyboard });
  });

  bot.command('help', async (ctx) => {
    await ctx.reply(
      '📋 *Доступные команды:*\n\n' +
      '/start — Открыть приложение\n' +
      '/search — Поиск недвижимости\n' +
      '/favorites — Избранное\n' +
      '/create — Подать объявление\n' +
      '/help — Помощь',
      { parse_mode: 'Markdown' }
    );
  });

  bot.catch((err) => {
    console.error('Bot error:', err);
  });

  return bot;
}

export function startBot() {
  if (!bot) return;
  bot.start({
    onStart: () => console.log('Telegram bot started'),
  });
}

export function getBot() {
  return bot;
}
