import React, { useState, useEffect, useCallback } from 'react';
import { Heart } from 'lucide-react';
import api from '../utils/api';
import ListingCard from '../components/ListingCard';

export default function FavoritesPage() {
  const [listings, setListings] = useState([]);
  const [loading, setLoading] = useState(true);

  const fetchFavorites = useCallback(async () => {
    try {
      const { data } = await api.get('/favorites');
      setListings(data.items.map((l) => ({ ...l, isFavorite: true })));
    } catch (err) {
      console.error('Favorites error:', err);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    fetchFavorites();
  }, [fetchFavorites]);

  const handleFavoriteToggle = (listingId) => {
    setListings((prev) => prev.filter((l) => l.id !== listingId));
  };

  if (loading) {
    return (
      <>
        <div className="header">
          <span className="header-title">Избранное</span>
        </div>
        <div className="loading"><div className="spinner" /></div>
      </>
    );
  }

  return (
    <>
      <div className="header">
        <span className="header-title">Избранное</span>
        <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>{listings.length}</span>
      </div>

      {listings.length === 0 ? (
        <div className="empty-state">
          <Heart size={64} />
          <div className="empty-state-title">Нет избранных</div>
          <div className="empty-state-text">
            Добавляйте понравившиеся объекты в избранное
          </div>
        </div>
      ) : (
        listings.map((listing) => (
          <ListingCard
            key={listing.id}
            listing={listing}
            onFavoriteToggle={handleFavoriteToggle}
          />
        ))
      )}
    </>
  );
}
