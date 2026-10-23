# Деплой: куда и как поставить софт

Три рабочих варианта по возрастанию надёжности. Все дают одно и то же:
ферма + бот-панель, ссылки в Telegram и в `data/`.

## Вариант A — Windows, эта же машина

```powershell
cd C:\linkforge
.venv\Scripts\activate
python -m linkforge bot -m 240
```

Панель работает, пока открыто окно. Для автозапуска — Планировщик задач:

```powershell
$action  = New-ScheduledTaskAction -Execute "C:\linkforge\.venv\Scripts\python.exe" -Argument "-u -m linkforge bot -m 1440" -WorkingDirectory "C:\linkforge"
$trigger = New-ScheduledTaskTrigger -AtLogon
Register-ScheduledTask -TaskName "LinkForge" -Action $action -Trigger $trigger -RunLevel Highest
```

`-m 1440` = 24 часа. Перезапуск вручную — кнопками в боте или задачей.

## Вариант B — Linux VPS (systemd, рекомендую)

Индийский selfcare лояльнее к одним и тем же IP, а VPS не спит и не
перезагружается сам. Один bootstrap-скрипт:

```bash
git clone <репозиторий> /opt/linkforge
cd /opt/linkforge
sudo bash deploy/install.sh
sudo systemctl enable --now linkforge
```

`deploy/install.sh` поднимает venv, ставит пакет, регистрирует
`linkforge.service` (авто-рестарт, `Restart=always`).

После первого запуска:

```bash
journalctl -u linkforge -f          # логи фермы
sudo systemctl restart linkforge    # кнопка «перезапустить»
```

`results.db` и `links/` лежат в `/opt/linkforge` — бэкап = `tar` этой папки.

## Вариант C — Docker (любая машина с докером)

```bash
cd linkforge
mkdir -p data
docker compose up -d --build
docker compose logs -f linkforge
```

`data/` монтируется как том: база и ссылки переживают пересборку и
`docker compose down`. Обновление софта:

```bash
git pull
docker compose up -d --build
```

## Что переносить при переезде

1. `.env` — целиком (там и провайдер, и бот).
2. `results.db` — история охот и сохранённые Jio-сессии (`relink` по старым
   номерам работает только с этой базой).
3. `links/` — собранные ссылки.

Этих трёх файлов достаточно, чтобы поднять ферму на новом месте со всей
памятью.

## Прокси на сервере

На VPS в прокси обычно нет нужды (один стабильный IP). Если Jio начал резать
запросы — включай пул кнопкой в `Настройках`:

- `PROXY_SOURCE=file` + `PROXY_FILE=/opt/linkforge/proxies.txt` — по одному
  прокси в строке;
- тест-URL лучше `https://www.jio.com/api/jio-authenticate-service/authenticate/authJsonData`:
  чистый IP получает 200, забаненный 403 — фильтр отсекает именно бан.

## Мониторинг

- `/status` в боте — баланс, попытки, найдено, аптайм;
- `linkforge check -n 20` — классификация сохранённых ссылок;
- systemd: `systemctl status linkforge`; Docker: `docker ps`.

Баланс провайдера не упадёт ниже $0.20 незаметно: при пустом кошельке ферма
паркуется и пишет в лог «waiting for refunds», а не долбит API.
