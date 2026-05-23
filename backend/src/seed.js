import db from './database.js';
import { ListingModel } from './models/listing.js';

const MOSCOW_DISTRICTS = [
  'Арбат', 'Тверской', 'Пресненский', 'Хамовники', 'Басманный',
  'Замоскворечье', 'Якиманка', 'Таганский', 'Мещанский', 'Красносельский',
  'Дорогомилово', 'Раменки', 'Гагаринский', 'Академический', 'Черёмушки',
  'Бутырский', 'Алексеевский', 'Марьина Роща', 'Савёловский', 'Аэропорт',
];

const METRO_STATIONS = [
  'Арбатская', 'Тверская', 'Пушкинская', 'Киевская', 'Парк Культуры',
  'Курская', 'Таганская', 'Новослободская', 'Комсомольская', 'Белорусская',
  'Сокол', 'Динамо', 'Войковская', 'ВДНХ', 'Алексеевская',
  'Проспект Мира', 'Сухаревская', 'Кропоткинская', 'Октябрьская', 'Полянка',
  'Чистые пруды', 'Лубянка', 'Площадь Революции', 'Третьяковская', 'Новокузнецкая',
];

const STREETS = [
  'Тверская ул.', 'Арбат ул.', 'Новый Арбат ул.', 'Пречистенка ул.', 'Остоженка ул.',
  'Большая Садовая ул.', 'Малая Бронная ул.', 'Покровка ул.', 'Маросейка ул.',
  'Петровка ул.', 'Мясницкая ул.', 'Сретенка ул.', 'Бауманская ул.',
  'Ленинский пр-т', 'Кутузовский пр-т', 'Ломоносовский пр-т',
  'Профсоюзная ул.', 'Вернадского пр-т', 'Комсомольский пр-т',
  'Садовая-Кудринская ул.', 'Садовая-Триумфальная ул.',
];

const FEATURES = [
  'Балкон', 'Лоджия', 'Кондиционер', 'Стиральная машина', 'Посудомоечная машина',
  'Холодильник', 'Интернет', 'Телевизор', 'Мебель', 'Парковка',
  'Лифт', 'Консьерж', 'Охрана', 'Детская площадка', 'Ремонт',
];

const IMAGES = [
  'https://images.unsplash.com/photo-1502672260266-1c1ef2d93688?w=800',
  'https://images.unsplash.com/photo-1560448204-e02f11c3d0e2?w=800',
  'https://images.unsplash.com/photo-1522708323590-d24dbb6b0267?w=800',
  'https://images.unsplash.com/photo-1560185893-a55cbc8c57e8?w=800',
  'https://images.unsplash.com/photo-1484154218962-a197022b5858?w=800',
  'https://images.unsplash.com/photo-1493809842364-78817add7ffb?w=800',
  'https://images.unsplash.com/photo-1536376072261-38c75010e6c9?w=800',
  'https://images.unsplash.com/photo-1600585154340-be6161a56a0c?w=800',
  'https://images.unsplash.com/photo-1600596542815-ffad4c1539a9?w=800',
  'https://images.unsplash.com/photo-1600607687939-ce8a6c25118c?w=800',
];

function rand(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

function pick(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

function pickN(arr, n) {
  const shuffled = [...arr].sort(() => Math.random() - 0.5);
  return shuffled.slice(0, n);
}

// Moscow center coordinates
const MOSCOW_CENTER = { lat: 55.7558, lng: 37.6173 };

function randomCoord() {
  return {
    lat: MOSCOW_CENTER.lat + (Math.random() - 0.5) * 0.15,
    lng: MOSCOW_CENTER.lng + (Math.random() - 0.5) * 0.3,
  };
}

function generateRentListing(i) {
  const rooms = rand(1, 5);
  const area = rooms * rand(14, 25) + rand(5, 15);
  const totalFloors = rand(5, 30);
  const floor = rand(1, totalFloors);
  const district = pick(MOSCOW_DISTRICTS);
  const metro = pick(METRO_STATIONS);
  const street = pick(STREETS);
  const house = rand(1, 80);
  const coord = randomCoord();

  const basePrice = rooms === 1 ? rand(25, 60) : rooms === 2 ? rand(40, 90) : rooms === 3 ? rand(55, 130) : rand(70, 200);

  return {
    title: `${rooms}-комн. кв., ${area} м², ${floor}/${totalFloors} эт.`,
    description: `Сдаётся ${rooms}-комнатная квартира в районе ${district}. ` +
      `Квартира площадью ${area} м², расположена на ${floor} этаже ${totalFloors}-этажного дома. ` +
      `Рядом метро ${metro} (${rand(2, 15)} мин. пешком). ` +
      `В квартире есть всё необходимое для комфортного проживания.`,
    price: basePrice * 1000,
    pricePeriod: 'month',
    propertyType: 'apartment',
    dealType: 'rent',
    rooms,
    area,
    floor,
    totalFloors,
    address: `Москва, ${street}, д. ${house}`,
    city: 'Москва',
    district,
    metroStation: metro,
    metroDistance: rand(2, 15),
    latitude: coord.lat,
    longitude: coord.lng,
    images: pickN(IMAGES, rand(2, 5)),
    features: pickN(FEATURES, rand(3, 8)),
    source: 'seed',
    sourceId: `seed_rent_${i}`,
    phone: `+7${rand(900, 999)}${rand(1000000, 9999999)}`,
  };
}

function generateSaleListing(i) {
  const rooms = rand(1, 5);
  const area = rooms * rand(14, 25) + rand(5, 15);
  const totalFloors = rand(5, 30);
  const floor = rand(1, totalFloors);
  const district = pick(MOSCOW_DISTRICTS);
  const metro = pick(METRO_STATIONS);
  const street = pick(STREETS);
  const house = rand(1, 80);
  const coord = randomCoord();

  const basePrice = rooms === 1 ? rand(5, 12) : rooms === 2 ? rand(8, 18) : rooms === 3 ? rand(12, 28) : rand(18, 50);

  return {
    title: `${rooms}-комн. кв., ${area} м², ${floor}/${totalFloors} эт.`,
    description: `Продаётся ${rooms}-комнатная квартира в районе ${district}. ` +
      `Общая площадь ${area} м², ${floor} этаж из ${totalFloors}. ` +
      `Метро ${metro} в шаговой доступности (${rand(2, 15)} мин.). ` +
      `Квартира в хорошем состоянии, готова к заселению.`,
    price: basePrice * 1000000,
    pricePeriod: 'total',
    propertyType: 'apartment',
    dealType: 'sale',
    rooms,
    area,
    floor,
    totalFloors,
    address: `Москва, ${street}, д. ${house}`,
    city: 'Москва',
    district,
    metroStation: metro,
    metroDistance: rand(2, 15),
    latitude: coord.lat,
    longitude: coord.lng,
    images: pickN(IMAGES, rand(2, 5)),
    features: pickN(FEATURES, rand(3, 8)),
    source: 'seed',
    sourceId: `seed_sale_${i}`,
    phone: `+7${rand(900, 999)}${rand(1000000, 9999999)}`,
  };
}

console.log('Seeding database...');

// Clear existing seed data
db.prepare("DELETE FROM listings WHERE source = 'seed'").run();

// Generate listings
let count = 0;
for (let i = 0; i < 60; i++) {
  ListingModel.create(generateRentListing(i));
  count++;
}
for (let i = 0; i < 40; i++) {
  ListingModel.create(generateSaleListing(i));
  count++;
}

console.log(`Seeded ${count} listings`);

// Create demo user
const existingUser = db.prepare("SELECT * FROM users WHERE telegram_id = '000000'").get();
if (!existingUser) {
  db.prepare(
    "INSERT INTO users (telegram_id, username, first_name, last_name) VALUES ('000000', 'demo_user', 'Демо', 'Пользователь')"
  ).run();
  console.log('Created demo user');
}

console.log('Seed complete!');
