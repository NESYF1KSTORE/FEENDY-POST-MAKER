import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { MessageCircle } from 'lucide-react';
import api from '../utils/api';
import { timeAgo } from '../utils/format';

export default function ChatsPage({ user }) {
  const navigate = useNavigate();
  const [chats, setChats] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchChats = async () => {
      try {
        const { data } = await api.get('/chats');
        setChats(data);
      } catch (err) {
        console.error('Chats error:', err);
      }
      setLoading(false);
    };
    fetchChats();
  }, []);

  if (loading) {
    return (
      <>
        <div className="header"><span className="header-title">Сообщения</span></div>
        <div className="loading"><div className="spinner" /></div>
      </>
    );
  }

  return (
    <>
      <div className="header">
        <span className="header-title">Сообщения</span>
      </div>

      {chats.length === 0 ? (
        <div className="empty-state">
          <MessageCircle size={64} />
          <div className="empty-state-title">Нет сообщений</div>
          <div className="empty-state-text">
            Напишите продавцу или арендодателю, чтобы начать диалог
          </div>
        </div>
      ) : (
        chats.map((chat) => {
          const otherName = chat.buyer_id === user?.id ? chat.seller_name : chat.buyer_name;
          const initial = (otherName || '?')[0].toUpperCase();

          return (
            <div
              key={chat.id}
              className="chat-list-item"
              onClick={() => navigate(`/chat/${chat.id}`)}
            >
              <div className="chat-avatar">{initial}</div>
              <div className="chat-info">
                <div className="chat-info-top">
                  <span className="chat-name">{otherName || 'Пользователь'}</span>
                  {chat.last_message_at && (
                    <span className="chat-time">{timeAgo(chat.last_message_at)}</span>
                  )}
                </div>
                <div className="chat-last-message">
                  {chat.listing_title && (
                    <span style={{ color: 'var(--cian-blue)', marginRight: 4 }}>
                      {chat.listing_price ? `${(chat.listing_price / 1000).toFixed(0)}к ₽` : ''}
                    </span>
                  )}
                  {chat.last_message || 'Нет сообщений'}
                </div>
              </div>
              {chat.unread_count > 0 && (
                <div className="nav-badge" style={{ position: 'static' }}>
                  {chat.unread_count}
                </div>
              )}
            </div>
          );
        })
      )}
    </>
  );
}
