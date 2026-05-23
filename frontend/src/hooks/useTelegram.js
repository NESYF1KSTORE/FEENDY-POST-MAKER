import { useState, useEffect } from 'react';
import api, { setTelegramUserId } from '../utils/api';

const tg = window.Telegram?.WebApp;

export function useTelegram() {
  const [user, setUser] = useState(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (tg) {
      tg.ready();
      tg.expand();
      tg.enableClosingConfirmation();
    }

    const initUser = async () => {
      let telegramUser = tg?.initDataUnsafe?.user;

      if (!telegramUser) {
        telegramUser = {
          id: '000000',
          username: 'demo_user',
          first_name: 'Демо',
          last_name: 'Пользователь',
        };
      }

      setTelegramUserId(String(telegramUser.id));

      try {
        const { data } = await api.post('/users/auth', {
          telegramId: String(telegramUser.id),
          username: telegramUser.username,
          firstName: telegramUser.first_name,
          lastName: telegramUser.last_name,
        });
        setUser(data);
      } catch (err) {
        console.error('Auth error:', err);
      }

      setReady(true);
    };

    initUser();
  }, []);

  return {
    tg,
    user,
    ready,
    themeParams: tg?.themeParams || {},
    colorScheme: tg?.colorScheme || 'light',
  };
}
