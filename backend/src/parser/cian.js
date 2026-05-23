import axios from 'axios';
import * as cheerio from 'cheerio';
import { ListingModel } from '../models/listing.js';

const CIAN_BASE = 'https://www.cian.ru';

const HEADERS = {
  'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
  'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
  'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
};

const DEAL_TYPES = {
  rent: 'snyat',
  sale: 'kupit',
};

const PROPERTY_TYPES = {
  apartment: 'kvartiru',
  room: 'komnatu',
  house: 'dom',
  commercial: 'kommercheskuyu-nedvizhimost',
};

function buildSearchUrl({ dealType = 'rent', propertyType = 'apartment', city = 'moskva', page = 1 }) {
  const deal = DEAL_TYPES[dealType] || 'snyat';
  const prop = PROPERTY_TYPES[propertyType] || 'kvartiru';
  return `${CIAN_BASE}/${deal}-${prop}/${city}/?p=${page}`;
}

async function fetchPage(url) {
  try {
    const response = await axios.get(url, { headers: HEADERS, timeout: 15000 });
    return response.data;
  } catch (error) {
    console.error(`Error fetching ${url}:`, error.message);
    return null;
  }
}

function parseListingsFromHtml(html, dealType) {
  const $ = cheerio.load(html);
  const listings = [];

  $('[data-name="CardComponent"]').each((_i, el) => {
    try {
      const card = $(el);
      const titleEl = card.find('[data-name="LinkArea"] a').first();
      const title = titleEl.text().trim();
      const sourceUrl = titleEl.attr('href') || '';
      const sourceId = sourceUrl.match(/\/(\d+)\//)?.[1] || '';

      const priceText = card.find('[data-name="CardPrice"]').text().replace(/\s/g, '');
      const price = parseInt(priceText.replace(/[^\d]/g, ''), 10) || 0;

      const subtitle = card.find('[data-name="Subtitle"]').text().trim();
      const addressEl = card.find('[data-name="GeoLabel"]');
      const address = addressEl.text().trim();

      const roomsMatch = title.match(/(\d+)-комн/);
      const rooms = roomsMatch ? parseInt(roomsMatch[1], 10) : null;

      const areaMatch = title.match(/([\d,.]+)\s*м²/);
      const area = areaMatch ? parseFloat(areaMatch[1].replace(',', '.')) : null;

      const floorMatch = subtitle.match(/(\d+)\/(\d+)\s*эт/);
      const floor = floorMatch ? parseInt(floorMatch[1], 10) : null;
      const totalFloors = floorMatch ? parseInt(floorMatch[2], 10) : null;

      const metroEl = card.find('[data-name="Underground"]');
      const metroStation = metroEl.find('a').first().text().trim() || null;

      const imageEl = card.find('img[data-name="Image"]').first();
      const imageUrl = imageEl.attr('src') || '';
      const images = imageUrl ? [imageUrl] : [];

      if (title && price > 0) {
        listings.push({
          title,
          price,
          pricePeriod: dealType === 'rent' ? 'month' : 'total',
          propertyType: 'apartment',
          dealType,
          rooms,
          area,
          floor,
          totalFloors,
          address: address || 'Москва',
          city: 'Москва',
          metroStation,
          images,
          source: 'cian',
          sourceUrl,
          sourceId: sourceId ? `cian_${sourceId}` : null,
        });
      }
    } catch (err) {
      console.error('Error parsing card:', err.message);
    }
  });

  return listings;
}

export async function parseCian({ dealType = 'rent', propertyType = 'apartment', city = 'moskva', pages = 3 } = {}) {
  console.log(`Parsing Cian: ${dealType} ${propertyType} in ${city}, ${pages} pages...`);
  const allListings = [];

  for (let page = 1; page <= pages; page++) {
    const url = buildSearchUrl({ dealType, propertyType, city, page });
    console.log(`  Fetching page ${page}: ${url}`);

    const html = await fetchPage(url);
    if (!html) continue;

    const listings = parseListingsFromHtml(html, dealType);
    allListings.push(...listings);

    // Respectful delay between pages
    await new Promise((resolve) => setTimeout(resolve, 2000 + Math.random() * 3000));
  }

  console.log(`  Found ${allListings.length} listings`);

  let saved = 0;
  for (const listing of allListings) {
    try {
      if (listing.sourceId) {
        const existing = ListingModel.findById(listing.sourceId);
        if (existing) continue;
      }
      ListingModel.create(listing);
      saved++;
    } catch (err) {
      if (!err.message.includes('UNIQUE')) {
        console.error('Error saving listing:', err.message);
      }
    }
  }

  console.log(`  Saved ${saved} new listings`);
  return { total: allListings.length, saved };
}

// Run directly
if (process.argv[1] && process.argv[1].endsWith('cian.js')) {
  parseCian({ dealType: 'rent', pages: 2 })
    .then((result) => console.log('Done:', result))
    .catch((err) => console.error('Error:', err));
}
