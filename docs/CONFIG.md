# Конфигурация: все переменные `.env`

Незаданные необязательные переменные берут значения по умолчанию из
`linkforge/config.py`. Секреты в коммит не попадают — `.env` в `.gitignore`.

## Провайдер номеров (обязательно)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `PROVIDER` | `pvacodes` | `pvacodes` (HTML-кабинет), `stacked` (native API PVACodes), `grizzly` |
| `PVACODES_EMAIL` | — | логин на beta.pvacodes.com |
| `PVACODES_PASSWORD` | — | пароль |
| `PVACODES_SESSION_ID` | — | cookie-вход вместо логина/пароля (если задан, приоритетнее) |
| `PVACODES_SERVICE` | `Jio50` | очередь товаров через запятую: `Jio50,Jio14` |
| `PVACODES_COUNTRY` | `India` | страна номеров |

Альтернативные провайдеры (задаются при `PROVIDER=stacked` / `grizzly`):
`STACKED_API_KEY` + `STACKED_SERVICE` + `STACKED_COUNTRY`;
`GRIZZLY_API_KEY` + `GRIZZLY_SERVICE` + `GRIZZLY_COUNTRY`.

## Лимиты и поведение

| Переменная | По умолчанию | Описание |
|---|---|---|
| `MAX_PRICE` | `1.0` | потолок цены аренды, $ |
| `OTP_TIMEOUT_SECONDS` | `180` | сколько ждать SMS с кодом |
| `CONCURRENCY` | `2` | параллельных воркеров (в панели сейчас 1) |
| `DB_PATH` | `results.db` | SQLite: охоты + сессии |
| `LINKS_DIR` | `links` | куда писать файлы ссылок |

## Telegram

| Переменная | По умолчанию | Описание |
|---|---|---|
| `TG_BOT_TOKEN` | — | токен от @BotFather |
| `TG_CHAT_ID` | — | твой numeric id; бот игнорирует все остальные чаты |

## Прокси

| Переменная | По умолчанию | Описание |
|---|---|---|
| `PROXY_ENABLED` | `0` | выключено — весь трафик напрямую |
| `PROXY_SOURCE` | `file` | `file` (локальный список) или `proxygrab` (HTTP API) |
| `PROXY_FILE` | — | файл со списком, по одному `host:port` на строку |
| `PROXYGRAB_URL` / `PROXYGRAB_KEY` | — | для `PROXY_SOURCE=proxygrab` |
| `PROXY_POOL_SIZE` | `10` | сколько живых выходов держать |
| `PROXY_ROTATE_SECONDS` | `600` | период пересборки пула |
| `PROXY_PROTO` | — | `socks5` / `http` / пусто = любые |
| `PROXY_COUNTRY` | — | ISO-код, например `IN` |
| `PROXY_ANON` | `elite` | только анонимные |
| `PROXY_TEST_URL` | `https://www.jio.com/` | адрес для проверки живости |
| `PROXY_VERIFY` | `1` | отбраковка мёртвых реальным запросом |
| `PROXY_VERIFY_TIMEOUT` | `20` | сек на проверку |
| `PROXY_SESSIONS` | `0` | проксировать ли старые cookie-сессии (риск «угона» сессии) |

Совет: как тест-URL лучше
`https://www.jio.com/api/jio-authenticate-service/authenticate/authJsonData`
(чистый IP → 200, бан → 403).

## Возвраты денег

Механика в коде, не в `.env`, но полезно знать тайминги: неудачный OTP
планирует авто-отмену аренды через `OTP_FAIL_DELAY_SECONDS` (в конфиге;
стандартное значение ~7 минут), отмена при NOT_JIO — сразу. Возврат приходит
на баланс провайдера сам, ферма в это время паркуется и ждёт.
