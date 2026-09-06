# Полное руководство по API маркетплейса TG MRKT (`tgmrkt.io`)

Данный документ представляет собой исчерпывающую техническую спецификацию и руководство по REST API маркетплейса подарков Telegram MRKT (`https://tgmrkt.io` / `https://cdn.tgmrkt.io`).

---

## 1. Общие сведения и архитектура

- **Базовый URL API:** `https://api.tgmrkt.io/api/v1`
- **CDN / WebApp хост:** `https://cdn.tgmrkt.io`
- **Формат данных:** `application/json` (все тела запросов и ответов)
- **Основная валюта:** **TON** в единицах **nanoTON**  
  $$1 \text{ TON} = 1\,000\,000\,000 \text{ nanoTON} = 10^9 \text{ nanoTON}$$
- **Редкость (Rarity):** `rarityPerMille` ($1/1000$, например `5` = $0.5\%$, `20` = $2.0\%$).

---

## 2. Аутентификация и обязательные заголовки

Все запросы к закрытым и полузакрытым эндпоинтам требуют передачи токена доступа (UUID-строка).

### Способы передачи токена
1. Заголовок `Authorization: <TOKEN>`
2. Cookie `access_token=<TOKEN>`

Рекомендуется передавать оба варианта для полной совместимости с WAF и бэкендом.

### Обязательные HTTP заголовки (Browser Emulation)
Маркетплейс защищён проверками заголовков (Cloudflare / WAF). При запросах скриптами необходимо эмулировать браузерное окружение:

```http
Accept: application/json, text/plain, */*
Accept-Language: ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7
Authorization: <YOUR_TOKEN>
Cookie: access_token=<YOUR_TOKEN>
Content-Type: application/json
Origin: https://cdn.tgmrkt.io
Referer: https://cdn.tgmrkt.io/
User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36
sec-ch-ua: "Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"
sec-ch-ua-mobile: ?0
sec-ch-ua-platform: "macOS"
sec-fetch-dest: empty
sec-fetch-mode: cors
sec-fetch-site: same-site
```

---

## 3. Спецификация эндпоинтов

### 3.1. Листинг подарков в продаже (`POST /gifts/saling`)

Основной эндпоинт для сканирования витрины маркетплейса, поиска новых дешёвых предложений и вычисления флора.

- **URL:** `https://api.tgmrkt.io/api/v1/gifts/saling`
- **Метод:** `POST`

#### Тело запроса (Параметры фильтрации и пагинации):
```json
{
  "page": 0,
  "limit": 50,
  "order": {
    "direction": "asc",
    "field": "priceNanoTons"
  },
  "filter": {
    "backdropNames": ["Black"],
    "collectionNames": ["Fine Pen"],
    "modelNames": ["Tsar Gold"],
    "minPriceNanoTons": null,
    "maxPriceNanoTons": null,
    "statuses": ["SALING"]
  }
}
```

> **Совет:** Для получения общего листинга (всех свежих подарков по возрастанию цены) передайте пустой `filter: {}`.

#### Пример cURL:
```bash
curl -X POST 'https://api.tgmrkt.io/api/v1/gifts/saling' \
  -H 'Authorization: bf73c16d-eff6-471e-95fc-fee1ddbcf3a2' \
  -H 'Cookie: access_token=bf73c16d-eff6-471e-95fc-fee1ddbcf3a2' \
  -H 'Content-Type: application/json' \
  -H 'Origin: https://cdn.tgmrkt.io' \
  -H 'Referer: https://cdn.tgmrkt.io/' \
  --data-raw '{"page":0,"limit":20,"order":{"direction":"asc","field":"priceNanoTons"},"filter":{}}'
```

#### Пример ответа:
```json
{
  "items": [
    {
      "id": "66d3a8a0b01c4e001f3e9a11",
      "giftId": "5821059147243717301",
      "collectionName": "Lunar Snake",
      "collectionTitle": "Lunar Snake",
      "modelName": "Viper",
      "modelTitle": "Viper",
      "num": 4210,
      "priceNanoTons": 3876000000,
      "backdropName": "Black",
      "backdropColor": "#000000",
      "symbol": "Rare",
      "createdAt": "2026-09-06T14:10:00.000Z"
    }
  ],
  "total": 14205,
  "page": 0,
  "limit": 20
}
```

---

### 3.2. Список всех коллекций (`GET /gifts/collections`)

Возвращает список всех существующих коллекций подарков с их общим флором, объёмом торгов и метаданными.

- **URL:** `https://api.tgmrkt.io/api/v1/gifts/collections`
- **Метод:** `GET`
- **Тело запроса:** Отсутствует

#### Пример cURL:
```bash
curl 'https://api.tgmrkt.io/api/v1/gifts/collections' \
  -H 'Authorization: bf73c16d-eff6-471e-95fc-fee1ddbcf3a2' \
  -H 'Cookie: access_token=bf73c16d-eff6-471e-95fc-fee1ddbcf3a2' \
  -H 'Origin: https://cdn.tgmrkt.io' \
  -H 'Referer: https://cdn.tgmrkt.io/'
```

#### Пример ответа:
```json
[
  {
    "name": "Fine Pen",
    "title": "Fine Pen",
    "modelStickerThumbnailKey": "gifts/stickers/thumbnails/46696e652050656e5f5473617220476f6c64.webp",
    "originalImageKey": "gifts/collections/46696e652050656e.webp",
    "thumbnailImageKey": "gifts/collections/46696e652050656e-thumb.webp",
    "createdAt": "2026-08-06T01:32:52.790835Z",
    "floorPriceNanoTons": 8477179200,
    "floorPriceForGamesNanoTons": 8477179200,
    "volume": 2977225654358,
    "isNew": true,
    "craftable": false,
    "isHidden": false
  }
]
```

---

### 3.3. Флор конкретных моделей (`POST /gifts/models`)

Возвращает детальную статистику и минимальную цену (**Floor Price**) для каждой модели в запрошенных коллекциях.

- **URL:** `https://api.tgmrkt.io/api/v1/gifts/models`
- **Метод:** `POST`
- **Критическое ограничение:** **Не более 10 коллекций за один запрос!** Превышение лимита вызывает ошибку 400/500.

#### Тело запроса:
```json
{
  "collections": [
    "Fine Pen",
    "Algorithm Cup",
    "Intelligence Cup",
    "Astral Shard",
    "Berry Box",
    "Big Year"
  ]
}
```

#### Пример cURL:
```bash
curl -X POST 'https://api.tgmrkt.io/api/v1/gifts/models' \
  -H 'Authorization: bf73c16d-eff6-471e-95fc-fee1ddbcf3a2' \
  -H 'Cookie: access_token=bf73c16d-eff6-471e-95fc-fee1ddbcf3a2' \
  -H 'Content-Type: application/json' \
  -H 'Origin: https://cdn.tgmrkt.io' \
  -H 'Referer: https://cdn.tgmrkt.io/' \
  --data-raw '{"collections":["Fine Pen","Berry Box"]}'
```

#### Пример ответа:
```json
[
  {
    "collectionName": "Fine Pen",
    "collectionTitle": "Fine Pen",
    "modelName": "Coin Mint",
    "modelTitle": "Coin Mint",
    "modelStickerThumbnailKey": "gifts/stickers/thumbnails/46696e652050656e5f436f696e204d696e74.webp",
    "createdAt": "2026-08-06T06:30:34.573399Z",
    "rarityPerMille": 5,
    "rarityName": null,
    "volume": null,
    "floorPriceNanoTons": 79560000000,
    "cashbackCoef": null
  },
  {
    "collectionName": "Fine Pen",
    "collectionTitle": "Fine Pen",
    "modelName": "Redacted",
    "modelTitle": "Redacted",
    "modelStickerThumbnailKey": "gifts/stickers/thumbnails/46696e652050656e5f5265646163746564.webp",
    "createdAt": "2026-08-06T07:25:12.913899Z",
    "rarityPerMille": 5,
    "rarityName": null,
    "volume": null,
    "floorPriceNanoTons": 19380000000,
    "cashbackCoef": null
  }
]
```

> **Важно:** Поле `floorPriceNanoTons` может иметь значение `null`, если в данный момент на маркете нет ни одного активного лота данной модели.

---

### 3.4. Авторизация через Telegram WebApp (`POST /auth`)

Инициализация сессии через валидацию данных Telegram WebApp.

- **URL:** `https://api.tgmrkt.io/api/v1/auth`
- **Метод:** `POST`
- **Тело запроса:**
```json
{
  "initData": "query_id=...&user=...&auth_date=...&hash=..."
}
```
- **Ответ:** Токен доступа (UUID), используемый в последующих запросах как `Authorization` и `access_token`.

---

## 4. Алгоритм расчёта ликвидности и критерии покупки

Подарок признаётся **ликвидным для покупки**, если выполняется хотя бы одно из трёх условий:

1. **Критерий чёрного фона (Black Backdrop):**
   - У подарка чёрный фон: `backdropName == "Black"`.
   - Цена лота ниже текущего флора чёрного фона минимум на `MIN_TON_DIFF`:
     $$\text{Price} \le \text{BlackFloor} - \text{MIN\_TON\_DIFF}$$

2. **Критерий сверхнизкой цены (Cheap Price):**
   - Цена лота строго меньше фиксированного порога `CHEAP_PRICE_THRESHOLD` (по умолчанию `3.0 TON`):
     $$\text{Price} < \text{CHEAP\_PRICE\_THRESHOLD}$$

3. **Критерий флора конкретной модели (Model Floor):**
   - Для модели подарка известен текущий флор модели `ModelFloor` (из эндпоинта `/gifts/models`).
   - Цена лота ниже флора этой конкретной модели минимум на `MIN_TON_DIFF`:
     $$\text{Price} \le \text{ModelFloor} - \text{MIN\_TON\_DIFF}$$

---

## 5. Стратегия кэширования и защита от Rate Limit (429)

### Ограничения по частоте (Rate Limits)
- Лимит налагается как на **IP-адрес**, так и на **токен авторизации**.
- При превышении сервер возвращает HTTP `429 Too Many Requests`.
- Рекомендуемый штрафной кулдаун (penalty) при получении 429: **60 секунд**.

### Оптимизация частоты запросов
1. **Витрина (`/gifts/saling`):**
   - Сканируется с высокой частотой (например, раз в `0.5` сек).
   - Распределяется по пулу аккаунтов (`tokens.txt`) и прокси (`proxies.txt`).
2. **Флор чёрного фона:**
   - Вычисляется раз в $N$ сканов (например, каждые 10–40 циклов) фильтрованным запросом с `"backdropNames": ["Black"]`.
3. **Флор всех моделей (`/gifts/collections` + `/gifts/models`):**
   - **Нельзя запрашивать каждый цикл!**
   - Коллекций на маркете $>70$, что требует $\approx 8$ батч-запросов по 10 коллекций.
   - Оптимальный интервал обновления: **раз в 12 часов** (`MODEL_FLOOR_REFRESH_HOURS = 12.0`).
   - Между батчами делать паузу `0.2` сек для плавности сетевой нагрузки.

---

## 6. Пример асинхронного обновления флоров на Python

```python
import asyncio
from curl_cffi.requests import AsyncSession

BASE_URL = "https://api.tgmrkt.io/api/v1"

async def update_all_model_floors(session: AsyncSession, token: str) -> dict:
    headers = {
        "Authorization": token,
        "Cookie": f"access_token={token}",
        "Origin": "https://cdn.tgmrkt.io",
        "Referer": "https://cdn.tgmrkt.io/",
        "Content-Type": "application/json",
    }
    
    # 1. Получаем список всех коллекций
    resp = await session.get(f"{BASE_URL}/gifts/collections", headers=headers)
    collections = resp.json()
    collection_names = [c["name"] for c in collections if "name" in c]
    
    model_floors = {}
    
    # 2. Дробим на батчи по 10 штук
    for i in range(0, len(collection_names), 10):
        batch = collection_names[i:i + 10]
        m_resp = await session.post(
            f"{BASE_URL}/gifts/models",
            headers=headers,
            json={"collections": batch}
        )
        for item in m_resp.json():
            col = item.get("collectionName")
            mod = item.get("modelName")
            floor_nano = item.get("floorPriceNanoTons")
            if col and mod and floor_nano is not None:
                key = f"{col}:{mod}"
                model_floors[key] = int(floor_nano)
                
        await asyncio.sleep(0.2)  # Защита от 429
        
    return model_floors
```
