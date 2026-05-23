import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, Plus } from 'lucide-react';
import api from '../utils/api';

const PROPERTY_TYPES = [
  { value: 'apartment', label: 'Квартира' },
  { value: 'room', label: 'Комната' },
  { value: 'house', label: 'Дом' },
  { value: 'commercial', label: 'Коммерческая' },
];

const FEATURES_LIST = [
  'Балкон', 'Лоджия', 'Кондиционер', 'Стиральная машина', 'Посудомоечная машина',
  'Холодильник', 'Интернет', 'Телевизор', 'Мебель', 'Парковка',
  'Лифт', 'Консьерж', 'Охрана', 'Детская площадка', 'Ремонт',
];

export default function CreateListingPage() {
  const navigate = useNavigate();
  const [form, setForm] = useState({
    dealType: 'rent',
    propertyType: 'apartment',
    title: '',
    description: '',
    price: '',
    rooms: '',
    area: '',
    floor: '',
    totalFloors: '',
    address: '',
    district: '',
    metroStation: '',
    phone: '',
    features: [],
  });
  const [submitting, setSubmitting] = useState(false);

  const handleChange = (field, value) => {
    setForm((prev) => ({ ...prev, [field]: value }));
  };

  const handleFeatureToggle = (feature) => {
    setForm((prev) => ({
      ...prev,
      features: prev.features.includes(feature)
        ? prev.features.filter((f) => f !== feature)
        : [...prev.features, feature],
    }));
  };

  const autoTitle = () => {
    const rooms = form.rooms ? `${form.rooms}-комн. кв.` : 'Квартира';
    const area = form.area ? `, ${form.area} м²` : '';
    const floor = form.floor ? `, ${form.floor}${form.totalFloors ? `/${form.totalFloors}` : ''} эт.` : '';
    return `${rooms}${area}${floor}`;
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!form.price || !form.address) return;

    setSubmitting(true);
    try {
      const data = {
        ...form,
        title: form.title || autoTitle(),
        price: Number(form.price),
        rooms: form.rooms ? Number(form.rooms) : null,
        area: form.area ? Number(form.area) : null,
        floor: form.floor ? Number(form.floor) : null,
        totalFloors: form.totalFloors ? Number(form.totalFloors) : null,
        city: 'Москва',
      };

      const { data: listing } = await api.post('/listings', data);
      navigate(`/listing/${listing.id}`);
    } catch (err) {
      console.error('Create error:', err);
      alert('Ошибка при создании объявления');
    }
    setSubmitting(false);
  };

  return (
    <>
      <div className="header">
        <button className="header-back" onClick={() => navigate(-1)}>
          <ArrowLeft size={20} /> Назад
        </button>
        <span className="header-title">Новое объявление</span>
        <div />
      </div>

      <form onSubmit={handleSubmit} style={{ padding: 16 }}>
        <div className="filter-section" style={{ padding: 0, marginBottom: 16, border: 'none' }}>
          <div className="filter-section-title">Тип сделки</div>
          <div className="filter-chips">
            <button
              type="button"
              className={`filter-chip ${form.dealType === 'rent' ? 'active' : ''}`}
              onClick={() => handleChange('dealType', 'rent')}
            >
              Аренда
            </button>
            <button
              type="button"
              className={`filter-chip ${form.dealType === 'sale' ? 'active' : ''}`}
              onClick={() => handleChange('dealType', 'sale')}
            >
              Продажа
            </button>
          </div>
        </div>

        <div className="filter-section" style={{ padding: 0, marginBottom: 16, border: 'none' }}>
          <div className="filter-section-title">Тип недвижимости</div>
          <div className="filter-chips">
            {PROPERTY_TYPES.map(({ value, label }) => (
              <button
                key={value}
                type="button"
                className={`filter-chip ${form.propertyType === value ? 'active' : ''}`}
                onClick={() => handleChange('propertyType', value)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        <div className="form-group">
          <label className="form-label">Цена, ₽ *</label>
          <input
            className="form-input"
            type="number"
            placeholder={form.dealType === 'rent' ? 'в месяц' : 'общая стоимость'}
            value={form.price}
            onChange={(e) => handleChange('price', e.target.value)}
            required
          />
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          <div className="form-group">
            <label className="form-label">Комнат</label>
            <input
              className="form-input"
              type="number"
              placeholder="2"
              value={form.rooms}
              onChange={(e) => handleChange('rooms', e.target.value)}
            />
          </div>
          <div className="form-group">
            <label className="form-label">Площадь, м²</label>
            <input
              className="form-input"
              type="number"
              placeholder="55"
              value={form.area}
              onChange={(e) => handleChange('area', e.target.value)}
            />
          </div>
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          <div className="form-group">
            <label className="form-label">Этаж</label>
            <input
              className="form-input"
              type="number"
              placeholder="5"
              value={form.floor}
              onChange={(e) => handleChange('floor', e.target.value)}
            />
          </div>
          <div className="form-group">
            <label className="form-label">Всего этажей</label>
            <input
              className="form-input"
              type="number"
              placeholder="17"
              value={form.totalFloors}
              onChange={(e) => handleChange('totalFloors', e.target.value)}
            />
          </div>
        </div>

        <div className="form-group">
          <label className="form-label">Адрес *</label>
          <input
            className="form-input"
            type="text"
            placeholder="Москва, ул. Тверская, д. 12"
            value={form.address}
            onChange={(e) => handleChange('address', e.target.value)}
            required
          />
        </div>

        <div className="form-group">
          <label className="form-label">Район</label>
          <input
            className="form-input"
            type="text"
            placeholder="Тверской"
            value={form.district}
            onChange={(e) => handleChange('district', e.target.value)}
          />
        </div>

        <div className="form-group">
          <label className="form-label">Метро</label>
          <input
            className="form-input"
            type="text"
            placeholder="Тверская"
            value={form.metroStation}
            onChange={(e) => handleChange('metroStation', e.target.value)}
          />
        </div>

        <div className="form-group">
          <label className="form-label">Телефон</label>
          <input
            className="form-input"
            type="tel"
            placeholder="+7 900 123 45 67"
            value={form.phone}
            onChange={(e) => handleChange('phone', e.target.value)}
          />
        </div>

        <div className="form-group">
          <label className="form-label">Заголовок</label>
          <input
            className="form-input"
            type="text"
            placeholder={autoTitle()}
            value={form.title}
            onChange={(e) => handleChange('title', e.target.value)}
          />
        </div>

        <div className="form-group">
          <label className="form-label">Описание</label>
          <textarea
            className="form-textarea"
            placeholder="Расскажите подробнее о квартире..."
            value={form.description}
            onChange={(e) => handleChange('description', e.target.value)}
          />
        </div>

        <div className="filter-section" style={{ padding: 0, marginBottom: 16, border: 'none' }}>
          <div className="filter-section-title">Удобства</div>
          <div className="filter-chips">
            {FEATURES_LIST.map((feature) => (
              <button
                key={feature}
                type="button"
                className={`filter-chip ${form.features.includes(feature) ? 'active' : ''}`}
                onClick={() => handleFeatureToggle(feature)}
              >
                {feature}
              </button>
            ))}
          </div>
        </div>

        <button
          type="submit"
          className="btn btn-primary btn-full"
          disabled={submitting || !form.price || !form.address}
          style={{ marginTop: 8, marginBottom: 32 }}
        >
          <Plus size={18} />
          {submitting ? 'Публикация...' : 'Опубликовать объявление'}
        </button>
      </form>
    </>
  );
}
