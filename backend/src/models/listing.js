import db from '../database.js';

export const ListingModel = {
  findById(id) {
    return db.prepare('SELECT * FROM listings WHERE id = ?').get(id);
  },

  search({
    dealType = 'rent',
    propertyType,
    city = 'Москва',
    minPrice,
    maxPrice,
    rooms,
    minArea,
    maxArea,
    metro,
    district,
    sortBy = 'created_at',
    sortOrder = 'DESC',
    page = 1,
    limit = 20,
  } = {}) {
    const conditions = ['is_active = 1'];
    const params = [];

    if (dealType) {
      conditions.push('deal_type = ?');
      params.push(dealType);
    }
    if (propertyType) {
      conditions.push('property_type = ?');
      params.push(propertyType);
    }
    if (city) {
      conditions.push('city = ?');
      params.push(city);
    }
    if (minPrice) {
      conditions.push('price >= ?');
      params.push(Number(minPrice));
    }
    if (maxPrice) {
      conditions.push('price <= ?');
      params.push(Number(maxPrice));
    }
    if (rooms) {
      const roomList = String(rooms).split(',').map(Number);
      conditions.push(`rooms IN (${roomList.map(() => '?').join(',')})`);
      params.push(...roomList);
    }
    if (minArea) {
      conditions.push('area >= ?');
      params.push(Number(minArea));
    }
    if (maxArea) {
      conditions.push('area <= ?');
      params.push(Number(maxArea));
    }
    if (metro) {
      conditions.push('metro_station LIKE ?');
      params.push(`%${metro}%`);
    }
    if (district) {
      conditions.push('district LIKE ?');
      params.push(`%${district}%`);
    }

    const allowedSort = ['price', 'area', 'created_at'];
    const actualSort = allowedSort.includes(sortBy) ? sortBy : 'created_at';
    const actualOrder = sortOrder === 'ASC' ? 'ASC' : 'DESC';

    const offset = (Number(page) - 1) * Number(limit);

    const countSql = `SELECT COUNT(*) as total FROM listings WHERE ${conditions.join(' AND ')}`;
    const { total } = db.prepare(countSql).get(...params);

    const sql = `
      SELECT * FROM listings
      WHERE ${conditions.join(' AND ')}
      ORDER BY ${actualSort} ${actualOrder}
      LIMIT ? OFFSET ?
    `;
    const items = db.prepare(sql).all(...params, Number(limit), offset);

    return {
      items: items.map(this._parseJson),
      total,
      page: Number(page),
      pages: Math.ceil(total / Number(limit)),
    };
  },

  getByUser(userId, page = 1, limit = 20) {
    const offset = (page - 1) * limit;
    const { total } = db.prepare('SELECT COUNT(*) as total FROM listings WHERE user_id = ?').get(userId);
    const items = db.prepare('SELECT * FROM listings WHERE user_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?')
      .all(userId, limit, offset);
    return { items: items.map(this._parseJson), total, page, pages: Math.ceil(total / limit) };
  },

  create(data) {
    const stmt = db.prepare(`
      INSERT INTO listings (
        user_id, title, description, price, price_period, property_type, deal_type,
        rooms, area, floor, total_floors, address, city, district,
        metro_station, metro_distance, latitude, longitude, images, features,
        source, source_url, source_id, phone
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `);
    const result = stmt.run(
      data.userId || null,
      data.title,
      data.description || '',
      data.price,
      data.pricePeriod || 'month',
      data.propertyType || 'apartment',
      data.dealType || 'rent',
      data.rooms || null,
      data.area || null,
      data.floor || null,
      data.totalFloors || null,
      data.address,
      data.city || 'Москва',
      data.district || null,
      data.metroStation || null,
      data.metroDistance || null,
      data.latitude || null,
      data.longitude || null,
      JSON.stringify(data.images || []),
      JSON.stringify(data.features || []),
      data.source || 'user',
      data.sourceUrl || null,
      data.sourceId || null,
      data.phone || null
    );
    return this.findById(result.lastInsertRowid);
  },

  update(id, data) {
    const fields = [];
    const params = [];
    const allowed = [
      'title', 'description', 'price', 'price_period', 'property_type', 'deal_type',
      'rooms', 'area', 'floor', 'total_floors', 'address', 'city', 'district',
      'metro_station', 'metro_distance', 'latitude', 'longitude', 'phone', 'is_active',
    ];

    const camelToSnake = (s) => s.replace(/[A-Z]/g, (c) => '_' + c.toLowerCase());

    for (const [key, value] of Object.entries(data)) {
      const snakeKey = camelToSnake(key);
      if (allowed.includes(snakeKey)) {
        fields.push(`${snakeKey} = ?`);
        params.push(value);
      }
    }

    if (data.images) {
      fields.push('images = ?');
      params.push(JSON.stringify(data.images));
    }
    if (data.features) {
      fields.push('features = ?');
      params.push(JSON.stringify(data.features));
    }

    if (fields.length === 0) return this.findById(id);

    fields.push('updated_at = CURRENT_TIMESTAMP');
    params.push(id);

    db.prepare(`UPDATE listings SET ${fields.join(', ')} WHERE id = ?`).run(...params);
    return this.findById(id);
  },

  delete(id) {
    db.prepare('DELETE FROM listings WHERE id = ?').run(id);
  },

  incrementViews(id) {
    db.prepare('UPDATE listings SET views_count = views_count + 1 WHERE id = ?').run(id);
  },

  getMapListings({ city, dealType, minPrice, maxPrice, rooms, propertyType }) {
    const conditions = ['is_active = 1', 'latitude IS NOT NULL', 'longitude IS NOT NULL'];
    const params = [];

    if (city) { conditions.push('city = ?'); params.push(city); }
    if (dealType) { conditions.push('deal_type = ?'); params.push(dealType); }
    if (minPrice) { conditions.push('price >= ?'); params.push(Number(minPrice)); }
    if (maxPrice) { conditions.push('price <= ?'); params.push(Number(maxPrice)); }
    if (rooms) {
      const roomList = String(rooms).split(',').map(Number);
      conditions.push(`rooms IN (${roomList.map(() => '?').join(',')})`);
      params.push(...roomList);
    }
    if (propertyType) { conditions.push('property_type = ?'); params.push(propertyType); }

    return db.prepare(`
      SELECT id, title, price, rooms, area, latitude, longitude, images, address, deal_type, property_type
      FROM listings WHERE ${conditions.join(' AND ')} LIMIT 500
    `).all(...params).map(this._parseJson);
  },

  _parseJson(item) {
    if (!item) return item;
    try { item.images = JSON.parse(item.images || '[]'); } catch { item.images = []; }
    try { item.features = JSON.parse(item.features || '[]'); } catch { item.features = []; }
    return item;
  },
};
