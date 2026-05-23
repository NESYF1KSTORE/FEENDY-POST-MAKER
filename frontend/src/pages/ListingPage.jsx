import React, { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { ArrowLeft, Heart, Phone, MessageCircle, MapPin, Eye } from 'lucide-react';
import api from '../utils/api';
import { formatPrice, formatRooms, formatArea, formatFloor } from '../utils/format';

export default function ListingPage({ user }) {
  const { id } = useParams();
  const navigate = useNavigate();
  const [listing, setListing] = useState(null);
  const [loading, setLoading] = useState(true);
  const [showPhone, setShowPhone] = useState(false);

  useEffect(() => {
    const fetchListing = async () => {
      try {
        const { data } = await api.get(`/listings/${id}`);
        setListing(data);
      } catch (err) {
        console.error('Error:', err);
      }
      setLoading(false);
    };
    fetchListing();
  }, [id]);

  const handleFavorite = async () => {
    if (!listing) return;
    try {
      if (listing.isFavorite) {
        await api.delete(`/favorites/${listing.id}`);
      } else {
        await api.post(`/favorites/${listing.id}`);
      }
      setListing({ ...listing, isFavorite: !listing.isFavorite });
    } catch (err) {
      console.error('Favorite error:', err);
    }
  };

  const handleChat = async () => {
    if (!listing || !listing.user_id) return;
    try {
      const { data } = await api.post('/chats', { listingId: listing.id });
      navigate(`/chat/${data.id}`);
    } catch (err) {
      console.error('Chat error:', err);
    }
  };

  if (loading) {
    return (
      <div className="loading" style={{ height: '100vh' }}>
        <div className="spinner" />
      </div>
    );
  }

  if (!listing) {
    return (
      <div className="empty-state">
        <div className="empty-state-title">Объявление не найдено</div>
        <button className="btn btn-primary" onClick={() => navigate('/')}>На главную</button>
      </div>
    );
  }

  const images = listing.images || [];

  return (
    <>
      <div className="header">
        <button className="header-back" onClick={() => navigate(-1)}>
          <ArrowLeft size={20} /> Назад
        </button>
        <button
          className={`favorite-btn ${listing.isFavorite ? 'active' : ''}`}
          onClick={handleFavorite}
        >
          <Heart size={22} fill={listing.isFavorite ? 'currentColor' : 'none'} />
        </button>
      </div>

      {images.length > 0 && (
        <div className="detail-images">
          {images.map((img, i) => (
            <img key={i} src={img} alt={`Фото ${i + 1}`} />
          ))}
        </div>
      )}

      <div className="detail-info">
        <div className="detail-price">
          {formatPrice(listing.price, listing.deal_type)}
        </div>
        <div className="detail-title">
          {listing.rooms ? formatRooms(listing.rooms) + ' · ' : ''}
          {listing.area ? formatArea(listing.area) + ' · ' : ''}
          {listing.floor ? formatFloor(listing.floor, listing.total_floors) : ''}
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 4, color: 'var(--text-muted)', fontSize: 13, marginTop: 8 }}>
          <MapPin size={14} />
          {listing.address}
        </div>

        {listing.metro_station && (
          <div className="listing-card-metro" style={{ marginTop: 8 }}>
            <span className="metro-dot" />
            {listing.metro_station}
            {listing.metro_distance && ` · ${listing.metro_distance} мин. пешком`}
          </div>
        )}

        <div style={{ display: 'flex', alignItems: 'center', gap: 4, color: 'var(--text-muted)', fontSize: 12, marginTop: 8 }}>
          <Eye size={14} />
          {listing.views_count || 0} просмотров
        </div>
      </div>

      <div className="detail-meta" style={{ padding: '0 16px 16px' }}>
        {listing.rooms && (
          <div className="detail-meta-item">
            <span className="detail-meta-label">Комнаты</span>
            <span className="detail-meta-value">{listing.rooms}</span>
          </div>
        )}
        {listing.area && (
          <div className="detail-meta-item">
            <span className="detail-meta-label">Площадь</span>
            <span className="detail-meta-value">{listing.area} м²</span>
          </div>
        )}
        {listing.floor && (
          <div className="detail-meta-item">
            <span className="detail-meta-label">Этаж</span>
            <span className="detail-meta-value">{formatFloor(listing.floor, listing.total_floors)}</span>
          </div>
        )}
        {listing.district && (
          <div className="detail-meta-item">
            <span className="detail-meta-label">Район</span>
            <span className="detail-meta-value">{listing.district}</span>
          </div>
        )}
      </div>

      {listing.description && (
        <div className="detail-section">
          <div className="detail-section-title">Описание</div>
          <div className="detail-description">{listing.description}</div>
        </div>
      )}

      {listing.features && listing.features.length > 0 && (
        <div className="detail-section">
          <div className="detail-section-title">Удобства</div>
          <div className="detail-features">
            {listing.features.map((f, i) => (
              <span key={i} className="feature-tag">{f}</span>
            ))}
          </div>
        </div>
      )}

      <div className="detail-actions">
        {listing.phone && (
          <button className="detail-phone-btn" onClick={() => setShowPhone(!showPhone)}>
            <Phone size={18} />
            {showPhone ? listing.phone : 'Показать телефон'}
          </button>
        )}
        {listing.user_id && user && listing.user_id !== user.id && (
          <button className="detail-chat-btn" onClick={handleChat}>
            <MessageCircle size={18} />
            Написать
          </button>
        )}
      </div>
    </>
  );
}
