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

Инициализация и обновление сессионного токена пользователя через валидацию данных Telegram WebApp (`initData`).

- **URL:** `https://api.tgmrkt.io/api/v1/auth`
- **Метод:** `POST`

#### Тело запроса:
```json
{
  "data": "query_id=AAHpJVJhAAAAAOklUmHD17gy&user=%7B%22id%22%3A1632773609%2C%22first_name%22%3A%22efim%22%2C%22last_name%22%3A%22%22%2C%22username%22%3A%22ethmbo%22%2C%22language_code%22%3A%22ru%22%2C%22is_premium%22%3Atrue%2C%22allows_write_to_pm%22%3Atrue%2C%22photo_url%22%3A%22https%3A%5C%2F%5C%2Ft.me%5C%2Fi%5C%2Fuserpic%5C%2F320%5C%2FdZGURB4WbBnkF1HqSr9j4-csRsOp9D8Xu_LrzC7IE0s.svg%22%7D&auth_date=1788703871&signature=V2nSm90wxAR07QAyfRjOSQf0tjJSobnyKEPRvD408PTXQiqYxGeAAsYtzN7pe3FvgVXnxBAZZNsgFctciCbkDA&hash=8a75e1956041b10fae0da7060de999985bdf61d6c4ace09dd01f9b09e9a73bce",
  "photo": "https://t.me/i/userpic/320/dZGURB4WbBnkF1HqSr9j4-csRsOp9D8Xu_LrzC7IE0s.svg",
  "appId": null
}
```

#### Структура строки `data` (Telegram Mini App initData):
- `query_id` — уникальный идентификатор сессии веб-приложения Telegram.
- `user` — URL-encoded JSON с данными профиля Telegram (`id`, `first_name`, `username`, `language_code` и др.).
- `auth_date` — Unix timestamp момента генерации данных клиентом Telegram.
- `hash` — криптографический HMAC-SHA256 хеш, подписанный секретным ключом бота.
- `signature` — Ed25519/HMAC цифровая подпись Telegram.

#### Пример cURL:
```bash
curl -X POST 'https://api.tgmrkt.io/api/v1/auth' \
  -H 'accept: */*' \
  -H 'content-type: application/json' \
  -H 'origin: https://cdn.tgmrkt.io' \
  -H 'referer: https://cdn.tgmrkt.io/' \
  --data-raw '{"data":"query_id=...&user=...&auth_date=...&hash=...","photo":"https://t.me/...","appId":null}'
```

#### Пример ответа:
```json
{
  "token": "d3650243-715f-4fd5-a21c-1b6cb54d4c6d",
  "isFirstTime": false,
  "giftId": null,
  "profile": null
}
```
Полученный `token` (UUID) подставляется в `Authorization` и cookie `access_token`.

---

#### 💡 Способы автоматизации получения токенов:

1. **Автоматический рефреш при 401 (Полуавтоматический):**
   Пока строка `data` (initData) не устарела по `auth_date`, скрипт может при получении ошибки 401 автоматически отправлять запрос к `POST /auth`, забирать новый UUID `token` и сохранять его в `tokens.txt` без участия человека.

2. **Полная автоматизация 24/7 через Telegram MTProto (Telethon / Pyrogram):**
   Telegram генерирует валидный `initData` только внутри клиента Telegram при вызове `RequestAppWebView`.
   Подключив Telegram-клиент через Python-библиотеку `telethon`:
   ```python
   from telethon import TelegramClient
   from telethon.tl.functions.messages import RequestAppWebViewRequest
   from telethon.tl.types import InputBotAppShortName
   import urllib.parse
   import requests

   client = TelegramClient("session_name", api_id, api_hash)
   await client.start()

   # Запрос запуска WebApp у бота маркета
   bot = await client.get_input_entity("mrkt_bot")
   web_view = await client(RequestAppWebViewRequest(
       peer=bot,
       app=InputBotAppShortName(bot, "app"),
       platform="android",
   ))

   # web_view.url содержит: https://cdn.tgmrkt.io/#tgWebAppData=...
   parsed = urllib.parse.urlparse(web_view.url)
   fragment = urllib.parse.parse_qs(parsed.fragment)
   init_data = fragment.get("tgWebAppData", [""])[0]

   # Получаем токен MRKT
   r = requests.post(
       "https://api.tgmrkt.io/api/v1/auth",
       json={"data": init_data, "photo": "", "appId": None},
       headers={"Origin": "https://cdn.tgmrkt.io", "Referer": "https://cdn.tgmrkt.io/"}
   )
   token = r.json()["token"]
   # Токен готов к использованию!
   ```
   Этот подход работает полностью автономно на сервере без необходимости что-либо копировать вручную.

---

### 3.5. Текущий баланс пользователя (`GET /balance`)

Возвращает информацию о текущих балансах аккаунта (TON/Gram, Stars, бонусы, стейкинг).

- **URL:** `https://api.tgmrkt.io/api/v1/balance`
- **Метод:** `GET`
- **Тело запроса:** Отсутствует

#### Пример cURL:
```bash
curl 'https://api.tgmrkt.io/api/v1/balance' \
  -H 'Authorization: bf73c16d-eff6-471e-95fc-fee1ddbcf3a2' \
  -H 'Cookie: access_token=bf73c16d-eff6-471e-95fc-fee1ddbcf3a2' \
  -H 'Origin: https://cdn.tgmrkt.io' \
  -H 'Referer: https://cdn.tgmrkt.io/'
```

#### Пример ответа:
```json
{
  "soft": 0,
  "hard": 1374200000,
  "totalHard": 13400000000,
  "hardLocked": 0,
  "stars": 0,
  "starsFotWithdraw": 0,
  "spices": 0,
  "friendsCount": 0,
  "luckyBuyCards": 0,
  "stackingPoints": 0,
  "spaceMonkeysPoints": 0,
  "nanoUSDs": 0,
  "nanoUSDsLocked": 0,
  "giftStakingPoints": 0,
  "bonus": 0
}
```

#### Описание ключевых полей баланса:
| Поле | Тип | Описание |
| :--- | :--- | :--- |
| `hard` | `int` | **Доступный баланс TON в nanoTON** ($1\,374\,200\,000 = 1.3742\text{ TON}$). Именно он используется для покупки подарков. |
| `totalHard` | `int` | Общий баланс TON пользователя (включая заблокированные средства). |
| `hardLocked` | `int` | Заблокированные TON (например, активные биды или заморозка). |
| `stars` | `int` | Баланс Telegram Stars. |
| `starsFotWithdraw` | `int` | Stars, доступные для вывода. |
| `spices` | `int` | Внутренняя валюта / очки специй. |
| `nanoUSDs` | `int` | Баланс в nano-USD (для долларовых расчётов). |

---

### 3.6. Лента событий и история сделок (`POST /feed`)

Возвращает ленту рыночной активности маркетплейса в реальном времени: продажи, новые листинги, изменения цен и снятия лотов.

- **URL:** `https://api.tgmrkt.io/api/v1/feed`
- **Метод:** `POST`

#### Параметры пагинации и фильтрации (Тело запроса):
```json
{
  "count": 20,
  "cursor": "2dc37f41-eddd-4fe2-be06-904b0c3a513e",
  "collectionNames": [],
  "modelNames": [],
  "backdropNames": [],
  "number": null,
  "type": [],
  "minPrice": null,
  "maxPrice": null,
  "ordering": "Latest",
  "lowToHigh": false,
  "query": null
}
```

> **Механизм курсора (`cursor`):**
> Значение `cursor` — это UUID нижнего (самого старого в текущей выборке) события/подарка. Передавая полученный из предыдущего ответа `cursor`, клиент запрашивает следующую страницу истории. Для запроса самых свежих событий передайте `"cursor": null` или опустите его.

#### Типы событий (`type` в элементе ленты):
- `sale` — подарок успешно куплен покупателем за сумму `amount`.
- `listing` — подарок выставлен на продажу по цене `amount`.
- `change_price` — продавец изменил цену лота на `amount`.
- `unlisting` — подарок снят продавцом с продажи.
- `lucky_buy` — покупка через механизм Lucky Buy.

#### Пример cURL:
```bash
curl -X POST 'https://api.tgmrkt.io/api/v1/feed' \
  -H 'Authorization: bf73c16d-eff6-471e-95fc-fee1ddbcf3a2' \
  -H 'Cookie: access_token=bf73c16d-eff6-471e-95fc-fee1ddbcf3a2' \
  -H 'Content-Type: application/json' \
  -H 'Origin: https://cdn.tgmrkt.io' \
  -H 'Referer: https://cdn.tgmrkt.io/' \
  --data-raw '{"count":20,"cursor":null,"collectionNames":[],"modelNames":[],"backdropNames":[],"number":null,"type":[],"minPrice":null,"maxPrice":null,"ordering":"Latest","lowToHigh":false,"query":null}'
```

#### Пример ответа:
```json
{
  "items": [
    {
      "type": "sale",
      "id": "03509717-0054-45a2-a0c4-8eb9e14c8ca0",
      "amount": 33639600000,
      "date": "2026-09-06T14:15:23.009076Z",
      "gift": {
        "id": "3fc62fa7-6120-4e7f-8578-164bbed48102",
        "giftId": 5810168527520268969,
        "title": "Genie Lamp",
        "collectionName": "Genie Lamp",
        "modelName": "Sahara",
        "modelTitle": "Sahara",
        "backdropName": "Steel Grey",
        "number": 6500,
        "salePrice": 32585099999,
        "isOnSale": false
      }
    },
    {
      "type": "listing",
      "id": "a10dd6de-13a8-42f2-b415-e1f158f1d905",
      "amount": 5100000000,
      "date": "2026-09-06T14:15:26.550113Z",
      "gift": {
        "id": "8ea5d49d-58c1-4936-9e25-980ba476ba0b",
        "giftId": 5859231407821292798,
        "title": "Snow Mittens",
        "collectionName": "Snow Mittens",
        "modelName": "Mistletoe",
        "backdropName": "Khaki Green",
        "number": 39798,
        "salePrice": 5100000000,
        "isOnSale": true
      }
    }
  ],
  "cursor": "b8bd0b3c-18e1-47cc-a635-797b8bf8b548"
}
```

---

### 3.7. Получение подарков по списку ID (`POST /gifts/saling/by-ids`)

Позволяет точечно запросить подробную информацию и актуальный статус продажи сразу для одного или нескольких лотов по их UUID. Идеально подходит для мгновенной верификации статуса (продан / снят / активен) и получения точных цен без поиска по пагинации.

- **URL:** `https://api.tgmrkt.io/api/v1/gifts/saling/by-ids`
- **Метод:** `POST`

#### Тело запроса:
```json
{
  "ids": [
    "d9f8a69c-308d-4938-8cad-19f9624e263e"
  ]
}
```

#### Пример cURL:
```bash
curl -X POST 'https://api.tgmrkt.io/api/v1/gifts/saling/by-ids' \
  -H 'Authorization: 661fd8b7-9ad5-4fb7-b756-d31c7fbdd5c0' \
  -H 'Cookie: access_token=661fd8b7-9ad5-4fb7-b756-d31c7fbdd5c0' \
  -H 'Content-Type: application/json' \
  -H 'Origin: https://cdn.tgmrkt.io' \
  -H 'Referer: https://cdn.tgmrkt.io/' \
  --data-raw '{"ids":["d9f8a69c-308d-4938-8cad-19f9624e263e"]}'
```

#### Пример ответа:
```json
[
  {
    "id": "d9f8a69c-308d-4938-8cad-19f9624e263e",
    "exportDate": "2026-04-15T11:11:05Z",
    "receivedDate": "2026-03-27T17:39:29Z",
    "giftId": 5918017157777589504,
    "giftIdString": "5918017157777589504",
    "maxUpgradedCount": 482271,
    "totalUpgradedCount": 353819,
    "backdropColorsCenterColor": 7055740,
    "backdropColorsEdgeColor": 4094320,
    "backdropColorsTextColor": 14218725,
    "backdropColorsSymbolColor": 739379,
    "backdropName": "Pine Green",
    "backdropRarityPerMille": 15,
    "backdropRarityName": null,
    "modelName": "Spring Grove",
    "modelRarityPerMille": 30,
    "modelRarityName": null,
    "modelStickerKey": "gifts/stickers/4368696c6c20466c616d655f537072696e672047726f7665.json",
    "modelStickerThumbnailKey": "gifts/stickers/thumbnails/4368696c6c20466c616d655f537072696e672047726f7665.webp",
    "symbolName": "Horned Helm",
    "symbolRarityPerMille": null,
    "symbolRarityName": null,
    "symbolStickerKey": "gifts/symbols/4368696c6c20466c616d655f486f726e65642048656c6d.webp",
    "symbolStickerThumbnailKey": "gifts/symbols/thumbnails/4368696c6c20466c616d655f486f726e65642048656c6d.webp",
    "name": "ChillFlame-196993",
    "number": 196993,
    "title": "Chill Flame",
    "collectionName": "Chill Flame",
    "isOnAuction": false,
    "isOnSale": true,
    "salePrice": 4080000000,
    "salePriceWithoutFee": 4000000000,
    "salesCount": 1,
    "promoteEndAt": "0001-01-01T00:00:00",
    "isMine": false,
    "isGiveawayReceived": false,
    "nextResaleDate": "2026-04-15T11:11:05Z",
    "nextTransferDate": "2026-04-15T11:11:05Z",
    "isLocked": false,
    "isLockedForSale": false,
    "unlockDate": "2026-04-15T11:11:05Z",
    "nextGiveAvailableAt": "0001-01-01T00:00:00",
    "isOnPlatform": true,
    "premarketStatus": "None",
    "waitGiftUntil": null,
    "giftsCollectionId": null,
    "giftType": "Upgraded",
    "collectionTitle": "Chill Flame",
    "modelTitle": "Spring Grove",
    "luckyBuy": true,
    "regularGiftValidation": "None",
    "validateRegularGiftAt": null,
    "isSpaceMonkey": false,
    "returnLockedUntil": null,
    "returnLockReason": null,
    "spaceMonkeysPoints": null,
    "craftable": false,
    "floorPriceNanoTONsByCollection": 4069800000,
    "floorPriceNanoTONsByBackdropModel": null,
    "isCrafted": false,
    "tgCanBeCrafted": true,
    "minted": false,
    "staked": false,
    "stakedByMe": false
  }
]
```

#### Ключевые поля:
- `isOnSale`: `true`, если подарок прямо сейчас продаётся на маркете; `false`, если выкуплен или снят.
- `salePrice`: текущая цена продажи в nanoTON.
- `floorPriceNanoTONsByCollection`: актуальный флор коллекции подарка в nanoTON.
- `isMine`: `true`, если подарок принадлежит авторизованному аккаунту.

---

### 3.8. Покупка подарка с баланса маркета (`POST /gifts/buy`)

Основной эндпоинт для мгновенной автоматической покупки подарка. Списание средств происходит с внутреннего баланса TON (`hard`) авторизованного аккаунта.

- **URL:** `https://api.tgmrkt.io/api/v1/gifts/buy`
- **Метод:** `POST`

#### Тело запроса:
```json
{
  "ids": [
    "d9f8a69c-308d-4938-8cad-19f9624e263e"
  ],
  "prices": {
    "d9f8a69c-308d-4938-8cad-19f9624e263e": 4080000000
  }
}
```

> **Важно:** В объекте `prices` необходимо передать маппинг `{ "<GIFT_ID>": <PRICE_IN_NANOTON> }`. Это защищает от покупки в случае, если продавец резко повысил цену перед вашей транзакцией (Slippage protection).

#### Пример cURL:
```bash
curl -X POST 'https://api.tgmrkt.io/api/v1/gifts/buy' \
  -H 'Authorization: 661fd8b7-9ad5-4fb7-b756-d31c7fbdd5c0' \
  -H 'Cookie: access_token=661fd8b7-9ad5-4fb7-b756-d31c7fbdd5c0' \
  -H 'Content-Type: application/json' \
  -H 'Origin: https://cdn.tgmrkt.io' \
  -H 'Referer: https://cdn.tgmrkt.io/' \
  --data-raw '{"ids":["d9f8a69c-308d-4938-8cad-19f9624e263e"],"prices":{"d9f8a69c-308d-4938-8cad-19f9624e263e":4080000000}}'
```

#### Пример успешного ответа:
```json
[
  {
    "type": "gift",
    "userGift": {
      "id": "d9f8a69c-308d-4938-8cad-19f9624e263e",
      "exportDate": "2026-04-15T11:11:05Z",
      "receivedDate": "2026-03-27T17:39:29Z",
      "giftId": 5918017157777589504,
      "giftIdString": "5918017157777589504",
      "name": "ChillFlame-196993",
      "number": 196993,
      "title": "Chill Flame",
      "collectionName": "Chill Flame",
      "modelName": "Spring Grove",
      "backdropName": "Pine Green",
      "isOnSale": false,
      "salePrice": 4080000000,
      "salePriceWithoutFee": 4000000000,
      "isMine": true,
      "isOnPlatform": true,
      "floorPriceNanoTONsByCollection": 4069800000
    },
    "price": 4080000000,
    "priceWithoutFee": 4000000000,
    "source": {
      "type": "buy_gift"
    },
    "collectionName": null,
    "modelName": null,
    "backdropName": null
  }
]
```

#### Возможные ошибки при покупке:
- **HTTP 400 Bad Request:** Недостаточно средств на балансе (`Not enough balance`) или цена лота изменилась/не совпадает с переданной в `prices`.
- **HTTP 404 / 409 Conflict:** Подарок уже выкуплен другим пользователем или снят продавцом с продажи.
- **HTTP 401 Unauthorized:** Истёк токен авторизации (требуется обновление сессии).

---

### 3.9. Выставление подарка на продажу (`POST /gifts/sale`)

Позволяет выставить один или несколько принадлежащих пользователю подарков на продажу на маркетплейсе по заданной цене.

- **URL:** `https://api.tgmrkt.io/api/v1/gifts/sale`
- **Метод:** `POST`

#### Тело запроса:
```json
{
  "ids": [
    "d9f8a69c-308d-4938-8cad-19f9624e263e"
  ],
  "price": 4200000000
}
```

> **Особенности расчёта комиссии маркетплейса (Fee Calculation):**
> - В поле `price` передаётся **желаемая чистая сумма**, которую продавец получит на баланс при продаже (в nanoTON, например `4200000000` = `4.20 TON`).
> - Маркетплейс автоматически добавляет сервисную комиссию **2%**:
>   $$\text{PublicPrice} = \text{price} \times 1.02 = 4\,200\,000\,000 \times 1.02 = 4\,284\,000\,000 \text{ nanoTON (4.284 TON)}$$
> - В ответе сервера в массиве `prices` возвращаются итоговые публичные цены, с которыми лоты размещены на витрине.

#### Пример cURL:
```bash
curl -X POST 'https://api.tgmrkt.io/api/v1/gifts/sale' \
  -H 'Authorization: 661fd8b7-9ad5-4fb7-b756-d31c7fbdd5c0' \
  -H 'Cookie: access_token=661fd8b7-9ad5-4fb7-b756-d31c7fbdd5c0' \
  -H 'Content-Type: application/json' \
  -H 'Origin: https://cdn.tgmrkt.io' \
  -H 'Referer: https://cdn.tgmrkt.io/' \
  --data-raw '{"ids":["d9f8a69c-308d-4938-8cad-19f9624e263e"],"price":4200000000}'
```

#### Пример ответа:
```json
{
  "ids": [
    "d9f8a69c-308d-4938-8cad-19f9624e263e"
  ],
  "prices": [
    4284000000
  ]
}
```

---

### 3.10. Мои подарки: Инвентарь и выставленные лоты (`POST /gifts`)

Возвращает список подарков текущего авторизованного пользователя. Позволяет разделять подарки, находящиеся в Хранилище (инвентаре), и подарки, уже выставленные на витрину маркета.

- **URL:** `https://api.tgmrkt.io/api/v1/gifts`
- **Метод:** `POST`

#### Ключевой параметр фильтрации:
- `"isListed": true` — возвращает **только активные лоты пользователя, выставленные на продажу**.
- `"isListed": false` — возвращает **только подарки в Хранилище/инвентаре** (не выставленные на продажу).
- `"isListed": null` — возвращает все подарки пользователя независимо от статуса.

#### Тело запроса:
```json
{
  "isListed": true,
  "count": 20,
  "cursor": "",
  "collectionNames": [],
  "modelNames": [],
  "backdropNames": [],
  "symbolNames": [],
  "number": null,
  "isNew": null,
  "isPremarket": null,
  "luckyBuy": null,
  "giftType": null,
  "craftable": null,
  "isCrafted": null,
  "tgCanBeCraftedFrom": null,
  "removeSelfSales": null,
  "isTransferable": null,
  "availableForStaking": null,
  "forGame": null,
  "minPrice": null,
  "maxPrice": null,
  "ordering": "None",
  "lowToHigh": false,
  "query": null
}
```

#### Пример cURL:
```bash
curl -X POST 'https://api.tgmrkt.io/api/v1/gifts' \
  -H 'Authorization: 661fd8b7-9ad5-4fb7-b756-d31c7fbdd5c0' \
  -H 'Cookie: access_token=661fd8b7-9ad5-4fb7-b756-d31c7fbdd5c0' \
  -H 'Content-Type: application/json' \
  -H 'Origin: https://cdn.tgmrkt.io' \
  -H 'Referer: https://cdn.tgmrkt.io/' \
  --data-raw '{"isListed":true,"count":20,"cursor":"","collectionNames":[],"modelNames":[],"backdropNames":[],"symbolNames":[],"number":null,"ordering":"None","lowToHigh":false}'
```

#### Пример ответа:
```json
{
  "gifts": [
    {
      "id": "d9f8a69c-308d-4938-8cad-19f9624e263e",
      "exportDate": "2026-04-15T11:11:05Z",
      "receivedDate": "2026-03-27T17:39:29Z",
      "giftId": 5918017157777589504,
      "giftIdString": "5918017157777589504",
      "name": "ChillFlame-196993",
      "number": 196993,
      "title": "Chill Flame",
      "collectionName": "Chill Flame",
      "modelName": "Spring Grove",
      "backdropName": "Pine Green",
      "isOnSale": true,
      "salePrice": 4284000000,
      "salePriceWithoutFee": 0,
      "salesCount": 1,
      "isMine": true,
      "isOnPlatform": true,
      "floorPriceNanoTONsByCollection": 4059600000
    }
  ],
  "cursor": null,
  "total": 1
}
```

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
