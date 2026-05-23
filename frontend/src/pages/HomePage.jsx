import React, { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { Search, SlidersHorizontal } from 'lucide-react';
import api from '../utils/api';
import ListingCard from '../components/ListingCard';
import FilterPanel from '../components/FilterPanel';

export default function HomePage() {
  const navigate = useNavigate();
  const [listings, setListings] = useState([]);
  const [loading, setLoading] = useState(true);
  const [dealType, setDealType] = useState('rent');
  const [showFilters, setShowFilters] = useState(false);
  const [filters, setFilters] = useState({});
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pages, setPages] = useState(1);
  const [searchQuery, setSearchQuery] = useState('');
  const [sortBy, setSortBy] = useState('created_at');

  const fetchListings = useCallback(async (resetPage = false) => {
    setLoading(true);
    const currentPage = resetPage ? 1 : page;
    try {
      const params = {
        dealType,
        page: currentPage,
        limit: 20,
        sortBy,
        ...filters,
      };
      if (searchQuery) params.metro = searchQuery;

      const { data } = await api.get('/listings', { params });
      if (resetPage) {
        setListings(data.items);
        setPage(1);
      } else {
        setListings((prev) => currentPage === 1 ? data.items : [...prev, ...data.items]);
      }
      setTotal(data.total);
      setPages(data.pages);
    } catch (err) {
      console.error('Fetch error:', err);
    }
    setLoading(false);
  }, [dealType, filters, page, searchQuery, sortBy]);

  useEffect(() => {
    fetchListings(true);
  }, [dealType, filters, sortBy]);

  const handleLoadMore = () => {
    if (page < pages) {
      setPage((p) => p + 1);
    }
  };

  useEffect(() => {
    if (page > 1) fetchListings();
  }, [page]);

  const handleFavoriteToggle = (listingId) => {
    setListings((prev) =>
      prev.map((l) => l.id === listingId ? { ...l, isFavorite: !l.isFavorite } : l)
    );
  };

  const handleFilterApply = (newFilters) => {
    setFilters(newFilters);
    setShowFilters(false);
  };

  const handleSearch = (e) => {
    e.preventDefault();
    fetchListings(true);
  };

  const activeFilterCount = Object.values(filters).filter(Boolean).length;

  return (
    <>
      <div className="header">
        <span className="header-logo">Циан</span>
        <button style={{ background: 'none', padding: 4 }} onClick={() => navigate('/create')}>
          <span style={{ fontSize: 13, color: 'var(--cian-blue)', fontWeight: 600 }}>+ Подать</span>
        </button>
      </div>

      <div className="deal-tabs">
        <button
          className={`deal-tab ${dealType === 'rent' ? 'active' : ''}`}
          onClick={() => setDealType('rent')}
        >
          Снять
        </button>
        <button
          className={`deal-tab ${dealType === 'sale' ? 'active' : ''}`}
          onClick={() => setDealType('sale')}
        >
          Купить
        </button>
      </div>

      <form className="search-bar" onSubmit={handleSearch}>
        <div className="search-input-wrapper">
          <Search className="search-icon" />
          <input
            className="search-input"
            placeholder="Метро, район, адрес"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
        </div>
        <button
          type="button"
          className={`filter-btn ${activeFilterCount > 0 ? 'active' : ''}`}
          onClick={() => setShowFilters(true)}
        >
          <SlidersHorizontal size={16} />
          {activeFilterCount > 0 && activeFilterCount}
        </button>
      </form>

      <div className="sort-bar">
        <span>{total} объявлений</span>
        <select
          className="sort-select"
          value={sortBy}
          onChange={(e) => setSortBy(e.target.value)}
        >
          <option value="created_at">По дате</option>
          <option value="price">По цене</option>
          <option value="area">По площади</option>
        </select>
      </div>

      {listings.map((listing) => (
        <ListingCard
          key={listing.id}
          listing={listing}
          onFavoriteToggle={handleFavoriteToggle}
        />
      ))}

      {loading && (
        <div className="loading">
          <div className="spinner" />
        </div>
      )}

      {!loading && page < pages && (
        <div style={{ padding: 16 }}>
          <button className="btn btn-outline btn-full" onClick={handleLoadMore}>
            Показать ещё
          </button>
        </div>
      )}

      {!loading && listings.length === 0 && (
        <div className="empty-state">
          <Search size={64} />
          <div className="empty-state-title">Ничего не найдено</div>
          <div className="empty-state-text">Попробуйте изменить параметры поиска</div>
        </div>
      )}

      {showFilters && (
        <FilterPanel
          filters={filters}
          onApply={handleFilterApply}
          onClose={() => setShowFilters(false)}
        />
      )}
    </>
  );
}
