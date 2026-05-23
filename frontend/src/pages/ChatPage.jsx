import React, { useState, useEffect, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { ArrowLeft, Send } from 'lucide-react';
import { io } from 'socket.io-client';
import api from '../utils/api';

export default function ChatPage({ user }) {
  const { id } = useParams();
  const navigate = useNavigate();
  const [messages, setMessages] = useState([]);
  const [text, setText] = useState('');
  const [loading, setLoading] = useState(true);
  const messagesEndRef = useRef(null);
  const socketRef = useRef(null);

  useEffect(() => {
    const fetchMessages = async () => {
      try {
        const { data } = await api.get(`/chats/${id}/messages`);
        setMessages(data.items);
      } catch (err) {
        console.error('Messages error:', err);
      }
      setLoading(false);
    };
    fetchMessages();

    const socketUrl = import.meta.env.VITE_API_URL
      ? import.meta.env.VITE_API_URL.replace('/api', '')
      : window.location.origin;

    const socket = io(socketUrl, {
      query: { telegramId: user?.telegram_id || '000000' },
    });

    socket.on('connect', () => {
      socket.emit('join_chat', Number(id));
    });

    socket.on('new_message', (message) => {
      setMessages((prev) => [...prev, message]);
    });

    socketRef.current = socket;

    return () => {
      socket.emit('leave_chat', Number(id));
      socket.disconnect();
    };
  }, [id, user]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const handleSend = async () => {
    if (!text.trim()) return;

    const messageText = text.trim();
    setText('');

    if (socketRef.current?.connected) {
      socketRef.current.emit('send_message', { chatId: Number(id), text: messageText });
    } else {
      try {
        const { data } = await api.post(`/chats/${id}/messages`, { text: messageText });
        setMessages((prev) => [...prev, data]);
      } catch (err) {
        console.error('Send error:', err);
      }
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const formatTime = (dateStr) => {
    const date = new Date(dateStr);
    return date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
      <div className="header">
        <button className="header-back" onClick={() => navigate('/chats')}>
          <ArrowLeft size={20} /> Назад
        </button>
      </div>

      <div className="chat-messages">
        {loading ? (
          <div className="loading"><div className="spinner" /></div>
        ) : messages.length === 0 ? (
          <div className="empty-state">
            <div className="empty-state-text">Напишите первое сообщение</div>
          </div>
        ) : (
          messages.map((msg) => (
            <div
              key={msg.id}
              className={`chat-message ${msg.sender_id === user?.id ? 'sent' : 'received'}`}
            >
              <div>{msg.text}</div>
              <div className="chat-message-time">{formatTime(msg.created_at)}</div>
            </div>
          ))
        )}
        <div ref={messagesEndRef} />
      </div>

      <div className="chat-input-area">
        <input
          className="chat-input"
          placeholder="Сообщение..."
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
        />
        <button className="chat-send-btn" onClick={handleSend} disabled={!text.trim()}>
          <Send size={18} />
        </button>
      </div>
    </div>
  );
}
