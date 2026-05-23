export function formatPrice(price, dealType) {
  if (!price) return '';
  const formatted = new Intl.NumberFormat('ru-RU').format(price);
  if (dealType === 'rent') {
    return `${formatted} ₽/мес`;
  }
  if (price >= 1000000) {
    const millions = (price / 1000000).toFixed(1).replace('.0', '');
    return `${millions} млн ₽`;
  }
  return `${formatted} ₽`;
}

export function formatArea(area) {
  if (!area) return '';
  return `${area} м²`;
}

export function formatFloor(floor, totalFloors) {
  if (!floor) return '';
  if (totalFloors) return `${floor}/${totalFloors} эт.`;
  return `${floor} эт.`;
}

export function formatRooms(rooms) {
  if (!rooms) return 'Студия';
  if (rooms === 1) return '1-комн.';
  if (rooms === 2) return '2-комн.';
  if (rooms === 3) return '3-комн.';
  if (rooms === 4) return '4-комн.';
  return `${rooms}-комн.`;
}

export function timeAgo(dateStr) {
  const date = new Date(dateStr);
  const now = new Date();
  const diff = Math.floor((now - date) / 1000);

  if (diff < 60) return 'только что';
  if (diff < 3600) return `${Math.floor(diff / 60)} мин. назад`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} ч. назад`;
  if (diff < 604800) return `${Math.floor(diff / 86400)} дн. назад`;
  return date.toLocaleDateString('ru-RU');
}
