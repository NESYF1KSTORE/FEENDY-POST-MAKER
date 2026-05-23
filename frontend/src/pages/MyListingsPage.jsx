import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, Home, Plus } from 'lucide-react';
import api from '../utils/api';
import ListingCard from '../components/ListingCard';

export default function MyListingsPage() {
  const navigate = useNavigate();
  const [listings, setListings] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchMyListings = async () => {
      try {
        const { data } = await api.get('/listings/my');
        setListings(data.items);
      } catch (err) {
        console.error('My listings error:', err);
      }
      setLoading(false);
    };
    fetchMyListings();
  }, []);

  return (
    <>
      <div className="header">
        <button className="header-back" onClick={() => navigate('/profile')}>
          <ArrowLeft size={20} /> Назад
        </button>
        <span className="header-title">Мои объявления</span>
        <button style={{ background: 'none', padding: 4 }} onClick={() => navigate('/create')}>
          <Plus size={22} color="var(--cian-blue)" />
        </button>
      </div>

      {loading ? (
        <div className="loading"><div className="spinner" /></div>
      ) : listings.length === 0 ? (
        <div className="empty-state">
          <Home size={64} />
          <div className="empty-state-title">Нет объявлений</div>
          <div className="empty-state-text">
            Подайте первое объявление о продаже или аренде недвижимости
          </div>
          <button
            className="btn btn-primary"
            style={{ marginTop: 16 }}
            onClick={() => navigate('/create')}
          >
            <Plus size={18} />
            Подать объявление
          </button>
        </div>
      ) : (
        listings.map((listing) => (
          <ListingCard key={listing.id} listing={listing} />
        ))
      )}
    </>
  );
}
