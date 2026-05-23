import React, { useState } from 'react';
import { X } from 'lucide-react';

const PROPERTY_TYPES = [
  { value: 'apartment', label: 'Квартира' },
  { value: 'room', label: 'Комната' },
  { value: 'house', label: 'Дом' },
  { value: 'commercial', label: 'Коммерческая' },
];

const ROOM_OPTIONS = [
  { value: '1', label: '1' },
  { value: '2', label: '2' },
  { value: '3', label: '3' },
  { value: '4', label: '4+' },
];

export default function FilterPanel({ filters, onApply, onClose }) {
  const [local, setLocal] = useState({ ...filters });

  const handleRoomToggle = (room) => {
    const current = local.rooms ? local.rooms.split(',') : [];
    const idx = current.indexOf(room);
    if (idx >= 0) {
      current.splice(idx, 1);
    } else {
      current.push(room);
    }
    setLocal({ ...local, rooms: current.join(',') });
  };

  const handleReset = () => {
    setLocal({
      propertyType: '',
      rooms: '',
      minPrice: '',
      maxPrice: '',
      minArea: '',
      maxArea: '',
      metro: '',
    });
  };

  return (
    <div className="filter-panel">
      <div className="filter-header">
        <h2 style={{ fontSize: 18, fontWeight: 700 }}>Фильтры</h2>
        <button onClick={onClose} style={{ background: 'none', padding: 4 }}>
          <X size={24} />
        </button>
      </div>

      <div className="filter-section">
        <div className="filter-section-title">Тип недвижимости</div>
        <div className="filter-chips">
          {PROPERTY_TYPES.map(({ value, label }) => (
            <button
              key={value}
              className={`filter-chip ${local.propertyType === value ? 'active' : ''}`}
              onClick={() => setLocal({ ...local, propertyType: local.propertyType === value ? '' : value })}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="filter-section">
        <div className="filter-section-title">Количество комнат</div>
        <div className="filter-chips">
          {ROOM_OPTIONS.map(({ value, label }) => (
            <button
              key={value}
              className={`filter-chip ${(local.rooms || '').split(',').includes(value) ? 'active' : ''}`}
              onClick={() => handleRoomToggle(value)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="filter-section">
        <div className="filter-section-title">Цена, ₽</div>
        <div className="filter-range">
          <input
            type="number"
            placeholder="от"
            value={local.minPrice || ''}
            onChange={(e) => setLocal({ ...local, minPrice: e.target.value })}
          />
          <span>—</span>
          <input
            type="number"
            placeholder="до"
            value={local.maxPrice || ''}
            onChange={(e) => setLocal({ ...local, maxPrice: e.target.value })}
          />
        </div>
      </div>

      <div className="filter-section">
        <div className="filter-section-title">Площадь, м²</div>
        <div className="filter-range">
          <input
            type="number"
            placeholder="от"
            value={local.minArea || ''}
            onChange={(e) => setLocal({ ...local, minArea: e.target.value })}
          />
          <span>—</span>
          <input
            type="number"
            placeholder="до"
            value={local.maxArea || ''}
            onChange={(e) => setLocal({ ...local, maxArea: e.target.value })}
          />
        </div>
      </div>

      <div className="filter-section">
        <div className="filter-section-title">Метро</div>
        <input
          className="form-input"
          type="text"
          placeholder="Название станции"
          value={local.metro || ''}
          onChange={(e) => setLocal({ ...local, metro: e.target.value })}
        />
      </div>

      <div className="filter-actions">
        <button className="btn btn-secondary" onClick={handleReset}>
          Сбросить
        </button>
        <button className="btn btn-primary" onClick={() => onApply(local)}>
          Показать
        </button>
      </div>
    </div>
  );
}
