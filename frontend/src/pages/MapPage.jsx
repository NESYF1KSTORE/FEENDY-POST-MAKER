import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { MapContainer, TileLayer, Marker, Popup } from 'react-leaflet';
import L from 'leaflet';
import api from '../utils/api';
import { formatPrice } from '../utils/format';

const defaultIcon = L.divIcon({
  className: 'custom-marker',
  html: '<div style="width:32px;height:32px;background:var(--cian-blue,#0468ff);border-radius:50%;border:3px solid white;box-shadow:0 2px 6px rgba(0,0,0,0.3);"></div>',
  iconSize: [32, 32],
  iconAnchor: [16, 32],
  popupAnchor: [0, -32],
});

const MOSCOW_CENTER = [55.7558, 37.6173];

export default function MapPage() {
  const navigate = useNavigate();
  const [listings, setListings] = useState([]);
  const [dealType, setDealType] = useState('rent');

  useEffect(() => {
    const fetchMapListings = async () => {
      try {
        const { data } = await api.get('/listings/map', { params: { dealType, city: 'Москва' } });
        setListings(data);
      } catch (err) {
        console.error('Map error:', err);
      }
    };
    fetchMapListings();
  }, [dealType]);

  return (
    <>
      <div className="header">
        <span className="header-logo">Карта</span>
        <div style={{ display: 'flex', gap: 8 }}>
          <button
            className={`filter-chip ${dealType === 'rent' ? 'active' : ''}`}
            onClick={() => setDealType('rent')}
            style={{ fontSize: 12 }}
          >
            Аренда
          </button>
          <button
            className={`filter-chip ${dealType === 'sale' ? 'active' : ''}`}
            onClick={() => setDealType('sale')}
            style={{ fontSize: 12 }}
          >
            Продажа
          </button>
        </div>
      </div>

      <div className="map-container">
        <MapContainer
          center={MOSCOW_CENTER}
          zoom={12}
          style={{ height: '100%', width: '100%' }}
          zoomControl={false}
        >
          <TileLayer
            url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
            attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
          />
          {listings.map((listing) => (
            listing.latitude && listing.longitude && (
              <Marker
                key={listing.id}
                position={[listing.latitude, listing.longitude]}
                icon={defaultIcon}
              >
                <Popup>
                  <div
                    className="map-popup"
                    style={{ cursor: 'pointer' }}
                    onClick={() => navigate(`/listing/${listing.id}`)}
                  >
                    <div className="map-popup-price">
                      {formatPrice(listing.price, listing.deal_type)}
                    </div>
                    <div className="map-popup-title">
                      {listing.rooms ? `${listing.rooms}-комн.` : ''} {listing.area ? `${listing.area} м²` : ''}
                    </div>
                    <div className="map-popup-address">{listing.address}</div>
                  </div>
                </Popup>
              </Marker>
            )
          ))}
        </MapContainer>
      </div>
    </>
  );
}
