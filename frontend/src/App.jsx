import React from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { useTelegram } from './hooks/useTelegram';
import BottomNav from './components/BottomNav';
import HomePage from './pages/HomePage';
import SearchPage from './pages/SearchPage';
import ListingPage from './pages/ListingPage';
import MapPage from './pages/MapPage';
import FavoritesPage from './pages/FavoritesPage';
import ChatsPage from './pages/ChatsPage';
import ChatPage from './pages/ChatPage';
import CreateListingPage from './pages/CreateListingPage';
import ProfilePage from './pages/ProfilePage';
import MyListingsPage from './pages/MyListingsPage';

export default function App() {
  const { user, ready } = useTelegram();

  if (!ready) {
    return (
      <div className="app-container">
        <div className="loading" style={{ height: '100vh' }}>
          <div className="spinner" />
        </div>
      </div>
    );
  }

  return (
    <BrowserRouter>
      <div className="app-container">
        <div className="page-content">
          <Routes>
            <Route path="/" element={<HomePage user={user} />} />
            <Route path="/search" element={<SearchPage user={user} />} />
            <Route path="/listing/:id" element={<ListingPage user={user} />} />
            <Route path="/map" element={<MapPage />} />
            <Route path="/favorites" element={<FavoritesPage user={user} />} />
            <Route path="/chats" element={<ChatsPage user={user} />} />
            <Route path="/chat/:id" element={<ChatPage user={user} />} />
            <Route path="/create" element={<CreateListingPage user={user} />} />
            <Route path="/profile" element={<ProfilePage user={user} />} />
            <Route path="/my-listings" element={<MyListingsPage user={user} />} />
          </Routes>
        </div>
        <BottomNav />
      </div>
    </BrowserRouter>
  );
}
