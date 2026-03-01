type CacheEntry<T> = {
  data: T;
  expiry: number;
};

class LocalCache<T> {
  private cache: Map<string, CacheEntry<T>> = new Map();

  constructor(private ttl: number) {} // TTL in milliseconds

  // 获取缓存中的数据
  get(key: string): T | null {
    const entry = this.cache.get(key);
    if (entry && Date.now() < entry.expiry) {
      return entry.data;
    }
    // 如果缓存过期或不存在，则返回 null
    this.cache.delete(key);
    return null;
  }

  // 设置缓存数据
  set(key: string, data: T): void {
    this.cache.set(key, {
      data,
      expiry: Date.now() + this.ttl,
    });
  }

  // 清除缓存
  clear(key: string): void {
    this.cache.delete(key);
  }
}

// 创建一个缓存实例，假设 TTL 为 1 小时
const cache = new LocalCache<any>(60 * 60 * 1000); // 1 hour TTL

// 通用缓存代理函数
async function cacheProxy<T>(
  key: string,
  fetchFunction: () => Promise<T>
): Promise<T> {
  // 尝试从缓存中获取数据
  const cachedData = cache.get(key);
  if (cachedData) {
    console.log(`Cache hit for key: ${key}`);
    return cachedData;
  }

  console.log(`Cache miss for key: ${key}, fetching new data...`);
  // 如果缓存中没有或者已经过期，调用传入的函数获取数据
  const data = await fetchFunction();
  // 将数据存入缓存
  cache.set(key, data);
  return data;
}

export { cacheProxy };
