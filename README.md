# 🏠 Циан — Telegram Mini App

Полноценный клон Циан в виде Telegram Mini App. Поиск и аренда/покупка недвижимости прямо в Telegram.

## Возможности

- 🔍 **Поиск** — по типу сделки, количеству комнат, цене, площади, метро, району
- 🗺 **Карта** — все объекты на интерактивной карте (Leaflet/OpenStreetMap)
- ❤️ **Избранное** — сохранение понравившихся объектов
- 💬 **Чат** — переписка с продавцами/арендодателями (Socket.IO, real-time)
- 📝 **Подача объявлений** — публикация своих объектов
- 📱 **Telegram Mini App** — нативная интеграция с Telegram WebApp SDK
- 🤖 **Telegram бот** — команды /start, /search, /favorites, /create, /help
- 🕷 **Парсер Циан** — автоматический сбор объявлений с cian.ru

## Стек

### Backend
- **Node.js** + **Express**
- **SQLite** (better-sqlite3) — лёгкая встроенная БД
- **Socket.IO** — real-time чат
- **Grammy** — Telegram Bot API
- **Cheerio** + **Axios** — парсинг Циан

### Frontend
- **React 18** + **Vite**
- **React Router** — маршрутизация
- **Leaflet** + **React-Leaflet** — интерактивная карта
- **Socket.IO Client** — real-time чат
- **Lucide React** — иконки
- **@twa-dev/sdk** — Telegram WebApp SDK

## Установка

### 1. Backend

```bash
cd backend
cp .env.example .env
# Заполните TELEGRAM_BOT_TOKEN в .env
npm install
npm run seed    # Наполнить базу тестовыми данными (100 объявлений)
npm run dev     # Запуск сервера на порту 3000
```

### 2. Frontend

```bash
cd frontend
npm install
npm run dev     # Запуск на порту 5173
```

### 3. Парсинг с Циан

```bash
cd backend
npm run parse   # Собрать объявления с cian.ru
```

## Структура

```
├── backend/
│   ├── src/
│   │   ├── index.js           # Express + Socket.IO сервер
│   │   ├── config.js          # Конфигурация
│   │   ├── database.js        # SQLite (схема, индексы)
│   │   ├── seed.js            # Генератор тестовых данных
│   │   ├── routes/
│   │   │   ├── listings.js    # CRUD объявлений + поиск + фильтры
│   │   │   ├── favorites.js   # Избранное
│   │   │   ├── chats.js       # Чаты и сообщения
│   │   │   └── users.js       # Авторизация через Telegram
│   │   ├── models/
│   │   │   ├── listing.js     # Модель объявления
│   │   │   ├── favorite.js    # Модель избранного
│   │   │   ├── chat.js        # Модель чата/сообщений
│   │   │   └── user.js        # Модель пользователя
│   │   ├── parser/
│   │   │   └── cian.js        # Парсер Циан
│   │   ├── bot/
│   │   │   └── telegram.js    # Telegram бот (Grammy)
│   │   └── middleware/
│   │       └── auth.js        # Авторизация
│   ├── package.json
│   └── .env.example
├── frontend/
│   ├── src/
│   │   ├── App.jsx            # Роутинг
│   │   ├── main.jsx           # Точка входа
│   │   ├── pages/
│   │   │   ├── HomePage.jsx       # Главная + поиск + фильтры
│   │   │   ├── ListingPage.jsx    # Детали объявления
│   │   │   ├── MapPage.jsx        # Карта
│   │   │   ├── FavoritesPage.jsx  # Избранное
│   │   │   ├── ChatsPage.jsx      # Список чатов
│   │   │   ├── ChatPage.jsx       # Чат (real-time)
│   │   │   ├── CreateListingPage.jsx  # Подача объявления
│   │   │   ├── ProfilePage.jsx    # Профиль
│   │   │   └── MyListingsPage.jsx # Мои объявления
│   │   ├── components/
│   │   │   ├── BottomNav.jsx      # Нижняя навигация
│   │   │   ├── ListingCard.jsx    # Карточка объявления
│   │   │   └── FilterPanel.jsx    # Панель фильтров
│   │   ├── hooks/
│   │   │   └── useTelegram.js     # Хук для Telegram WebApp
│   │   ├── utils/
│   │   │   ├── api.js             # Axios клиент
│   │   │   └── format.js          # Форматирование
│   │   └── styles/
│   │       └── global.css         # Стили в стиле Циан
│   ├── index.html
│   ├── vite.config.js
│   └── package.json
└── README-CIAN.md
```

## API Endpoints

### Объявления
- `GET /api/listings` — поиск с фильтрами
- `GET /api/listings/map` — объекты для карты
- `GET /api/listings/my` — мои объявления
- `GET /api/listings/:id` — детали
- `POST /api/listings` — создать
- `PATCH /api/listings/:id` — обновить
- `DELETE /api/listings/:id` — удалить

### Избранное
- `GET /api/favorites` — список избранного
- `POST /api/favorites/:listingId` — добавить
- `DELETE /api/favorites/:listingId` — удалить

### Чаты
- `GET /api/chats` — список чатов
- `POST /api/chats` — создать чат
- `GET /api/chats/:id/messages` — сообщения
- `POST /api/chats/:id/messages` — отправить
- `GET /api/chats/unread` — непрочитанные

### Пользователи
- `POST /api/users/auth` — авторизация
- `GET /api/users/me` — профиль

## Команды бота

- `/start` — Открыть Mini App
- `/search` — Поиск недвижимости
- `/favorites` — Избранное
- `/create` — Подать объявление
- `/help` — Помощь

## Деплой

1. Задеплойте backend (Render, Railway, VPS)
2. Соберите frontend: `cd frontend && npm run build`
3. Настройте `MINI_APP_URL` в .env бота
4. Зарегистрируйте Mini App через [@BotFather](https://t.me/BotFather) → /newapp

## Лицензия

MIT
