import React, { useState, useEffect } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { Search, Map, Heart, MessageCircle, User } from 'lucide-react';
import api from '../utils/api';

export default function BottomNav() {
  const navigate = useNavigate();
  const location = useLocation();
  const [unread, setUnread] = useState(0);

  useEffect(() => {
    const fetchUnread = async () => {
      try {
        const { data } = await api.get('/chats/unread');
        setUnread(data.unread);
      } catch {
        // ignore
      }
    };
    fetchUnread();
    const interval = setInterval(fetchUnread, 30000);
    return () => clearInterval(interval);
  }, []);

  const tabs = [
    { path: '/', icon: Search, label: 'Поиск' },
    { path: '/map', icon: Map, label: 'Карта' },
    { path: '/favorites', icon: Heart, label: 'Избранное' },
    { path: '/chats', icon: MessageCircle, label: 'Чаты', badge: unread },
    { path: '/profile', icon: User, label: 'Профиль' },
  ];

  return (
    <nav className="bottom-nav">
      {tabs.map(({ path, icon: Icon, label, badge }) => (
        <button
          key={path}
          className={`nav-item ${location.pathname === path ? 'active' : ''}`}
          onClick={() => navigate(path)}
        >
          <Icon />
          {label}
          {badge > 0 && <span className="nav-badge">{badge}</span>}
        </button>
      ))}
    </nav>
  );
}
