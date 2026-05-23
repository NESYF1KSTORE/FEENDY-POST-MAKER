import React from 'react';
import { useNavigate } from 'react-router-dom';
import { Home, Heart, PlusCircle, Settings, HelpCircle } from 'lucide-react';

export default function ProfilePage({ user }) {
  const navigate = useNavigate();

  const initial = user?.first_name?.[0] || user?.username?.[0] || '?';

  return (
    <>
      <div className="header">
        <span className="header-title">Профиль</span>
      </div>

      <div className="profile-header">
        <div className="profile-avatar">{initial.toUpperCase()}</div>
        <div>
          <div className="profile-name">
            {user?.first_name} {user?.last_name || ''}
          </div>
          {user?.username && (
            <div className="profile-username">@{user.username}</div>
          )}
        </div>
      </div>

      <div className="profile-menu">
        <button className="profile-menu-item" onClick={() => navigate('/my-listings')}>
          <Home />
          Мои объявления
        </button>
        <button className="profile-menu-item" onClick={() => navigate('/favorites')}>
          <Heart />
          Избранное
        </button>
        <button className="profile-menu-item" onClick={() => navigate('/create')}>
          <PlusCircle />
          Подать объявление
        </button>
        <button className="profile-menu-item" onClick={() => {}}>
          <Settings />
          Настройки
        </button>
        <button className="profile-menu-item" onClick={() => {}}>
          <HelpCircle />
          Помощь
        </button>
      </div>
    </>
  );
}
