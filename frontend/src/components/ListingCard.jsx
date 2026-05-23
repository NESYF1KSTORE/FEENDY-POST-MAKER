import React from 'react';
import { useNavigate } from 'react-router-dom';
import { Heart, MapPin } from 'lucide-react';
import { formatPrice, formatRooms, formatArea, formatFloor } from '../utils/format';
import api from '../utils/api';

export default function ListingCard({ listing, onFavoriteToggle }) {
  const navigate = useNavigate();
  const images = listing.images || [];
  const mainImage = images[0] || 'https://images.unsplash.com/photo-1560448204-e02f11c3d0e2?w=800';

  const handleFavorite = async (e) => {
    e.stopPropagation();
    try {
      if (listing.isFavorite) {
        await api.delete(`/favorites/${listing.id}`);
      } else {
        await api.post(`/favorites/${listing.id}`);
      }
      if (onFavoriteToggle) onFavoriteToggle(listing.id);
    } catch (err) {
      console.error('Favorite error:', err);
    }
  };

  return (
    <div className="listing-card" onClick={() => navigate(`/listing/${listing.id}`)}>
      <div className="listing-card-image">
        <img src={mainImage} alt={listing.title} loading="lazy" />
        {images.length > 1 && (
          <span className="image-count">{images.length} фото</span>
        )}
      </div>

      <div className="listing-card-price">
        {formatPrice(listing.price, listing.deal_type)}
      </div>

      <div className="listing-card-title">
        {listing.rooms ? formatRooms(listing.rooms) : ''}{' '}
        {listing.area ? formatArea(listing.area) : ''}{' '}
        {listing.floor ? formatFloor(listing.floor, listing.total_floors) : ''}
      </div>

      <div className="listing-card-address">
        <MapPin size={14} />
        {listing.address}
      </div>

      {listing.metro_station && (
        <div className="listing-card-metro">
          <span className="metro-dot" />
          {listing.metro_station}
          {listing.metro_distance && ` · ${listing.metro_distance} мин.`}
        </div>
      )}

      <div className="listing-card-actions">
        <button
          className={`favorite-btn ${listing.isFavorite ? 'active' : ''}`}
          onClick={handleFavorite}
        >
          <Heart size={20} fill={listing.isFavorite ? 'currentColor' : 'none'} />
        </button>
      </div>
    </div>
  );
}
