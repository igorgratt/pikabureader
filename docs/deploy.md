# Деплой PikaBuReader

## Содержание

1. [Быстрый старт](#1-быстрый-старт)
2. [Docker (рекомендуется)](#2-docker-рекомендуется)
3. [Прямой запуск на Python](#3-прямой-запуск-на-python)
4. [HTTPS + Let's Encrypt](#4-https--lets-encrypt)
5. [systemd-сервис](#5-systemd-сервис)
6. [Безопасность](#6-безопасность)
7. [Резервное копирование](#7-резервное-копирование)
8. [Обновление](#8-обновление)

---

## 1. Быстрый старт

```bash
git clone https://github.com/igorgratt/pikabureader.git
cd pikabureader
docker compose up -d
# Открой http://localhost:8000
```

---

## 2. Docker (рекомендуется)

### docker-compose.yml (уже в репозитории)

```yaml
services:
  app:
    build: .
    restart: unless-stopped
    ports:
      - "127.0.0.1:8000:8000"   # 127.0.0.1 — только локально; для внешнего доступа — убрать
    volumes:
      - ./data:/app/data        # книги, БД, обложки
    environment:
      - HOST=0.0.0.0
      - PORT=8000
      - SECRET_KEY=<свой_ключ>
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/healthz"]
      interval: 30s
      timeout: 10s
      retries: 3
```

> **Важно:** не запускайте порт 8000 наружу без пароля и HTTPS (см. раздел «Безопасность»).

### Запуск

```bash
docker compose up -d
docker compose ps
docker compose logs -f   # посмотреть логи
```

### Остановка

```bash
docker compose stop    # остановить, данные сохранятся
docker compose down     # удалить контейнер (данные в ./data — целы)
```

---

## 3. Прямой запуск на Python

Требуется: Python 3.11+, pip, git.

```bash
git clone https://github.com/igorgratt/pikabureader.git
cd pikabureader
pip install -r requirements.txt
#，首次设置 SECRET_KEY：
# Linux/macOS:
export SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
# Windows PowerShell:
$env:SECRET_KEY = python -c "import secrets; print(secrets.token_hex(32))"

HOST=0.0.0.0 PORT=8000 python app.py
```

Автозапуск — через systemd ([см. раздел 5](#5-systemd-сервис)).

---

## 4. HTTPS + Let's Encrypt

### Вариант A: Caddy (рекомендуется, автоматически HTTPS)

Caddy автоматически получает сертификат Let's Encrypt и обновляет его.

```bash
# docker-compose.yml дополняем:
services:
  caddy:
    image: caddy:2
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - ./data/caddy_data:/data
    depends_on:
      - app

  app:
    # ... existing config ...
    expose:
      - "8000"
```

```Caddyfile
# Caddyfile в корне проекта
your-domain.example.com {
    reverse_proxy localhost:8000
    encode gzip
}
```

```bash
# первый запуск — Caddy сам получит сертификат
docker compose up -d
```

### Вариант Б: Nginx + Certbot

```nginx
# /etc/nginx/sites-available/pikabu
server {
    listen 80;
    server_name your-domain.example.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl;
    server_name your-domain.example.com;

    ssl_certificate /etc/letsencrypt/live/your-domain.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
# Получить сертификат
sudo certbot --nginx -d your-domain.example.com
# Certbot обновляет сертификаты автоматически
```

---

## 5. systemd-сервис

Для прямого запуска (без Docker) — автозапуск при загрузке.

```ini
# /etc/systemd/system/pikabureader.service
[Unit]
Description=PikaBuReader
After=network.target

[Service]
Type=simple
User=pikabu
Group=pikabu
WorkingDirectory=/opt/pikabureader
Environment="HOST=0.0.0.0" "PORT=8000" "SECRET_KEY=<ваш_ключ>"
ExecStart=/opt/pikabureader/.venv/bin/python app.py
Restart=always
RestartSec=5

#_security_
NoNewPrivileges=true
PrivateTmp=true
ReadOnlyPaths=/opt/pikabureader

[Install]
WantedBy=multi-user.target
```

```bash
# установка
sudo nano /etc/systemd/system/pikabureader.service
sudo systemctl daemon-reload
sudo systemctl enable pikabureader
sudo systemctl start pikabureader
sudo systemctl status pikabureader

# проверка
curl http://localhost:8000/healthz
```

---

## 6. Безопасность

### Обязательно для публичного сервера

1. **Установить свой `SECRET_KEY`** — не дефолтный. При первом запуске без переменной
   `SECRET_KEY` PikaBuReader сгенерирует его автоматически, но при перезапуске он
   потеряется и все сессии станут невалидными. Сохраните ключ:

   ```bash
   # Linux
   echo "SECRET_KEY=$(python -c 'import secrets; print(secrets.token_hex(32))')" \
       >> /opt/pikabureader/.env

   # и добавьте в systemd:
   EnvironmentFile=/opt/pikabureader/.env
   ```

2. **Поставить пароль** — откройте `/admin` и задайте пароль доступа. Без пароля
   любой, кто знает адрес, имеет полный доступ к библиотеке.

3. **Закрыть порт файрволом** (если не нужен внешний доступ):

   ```bash
   sudo ufw allow ssh
   sudo ufw allow 443/tcp   # если нужен HTTPS извне
   sudo ufw enable
   ```

4. **fail2ban на `/login`** — защита от брутфорса:

   ```bash
   sudo apt install fail2ban
   ```

   ```python
   # /etc/fail2ban/jail.local
   [pikabu-login]
   enabled = true
   port = 8000
   filter = pikabu-login
   logpath = /opt/pikabureader/data/app.log
   maxretry = 10
   findtime = 600
   bantime = 3600
   ```

   ```
   # /etc/fail2ban/filter.d/pikabu-login.conf
   [Definition]
   failregex = ^.*POST /login.* 401
   ```

5. **Права на `data/`**:

   ```bash
   sudo chown -R pikabu:pikabu /opt/pikabureader/data
   chmod 600 /opt/pikabureader/data/secret_key   # Linux
   ```

6. **CSP и X-Frame-Options** — в Caddy/Nginx:

   ```caddy
   header / {
       Content-Security-Policy "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:;"
       X-Frame-Options DENY
       X-Content-Type-Options nosniff
   }
   ```

### Что уже реализовано

| Что | Статус |
|-----|--------|
| Хеширование паролей (pbkdf2) | ✅ |
| Сессии с подписью | ✅ |
| CSRF-токены | ✅ |
| Лимит на размер загрузки (200 МБ) | ✅ |
| SQLite без удалённого доступа | ✅ |
| Sanitisation HTML | ✅ |

---

## 7. Резервное копирование

```bash
# CLI (в любой ОС)
python pika.py backup --output /path/to/backup.zip

# или внутри Docker:
docker exec -it pikabureader-app-1 python pika.py backup --output /data/backup.zip
```

Рекомендуемый график (crontab):

```crontab
# ежедневно в 03:00
0 3 * * * docker exec pikabureader-app-1 python pika.py backup --output /data/backup-$(date +\%Y-\%m-\%d).zip

# удалять бэкапы старше 30 дней
0 4 * * * find /backups -name "backup-*.zip" -mtime +30 -delete
```

---

## 8. Обновление

```bash
git pull

# Docker
docker compose up -d --build

# Без Docker (активируй venv если есть)
pip install -r requirements.txt
# перезапусти сервис
sudo systemctl restart pikabureader
```

Тесты прогоняются в CI при каждом push — после pull убедись что всё зелёное:

```bash
python -m pytest tests/ -q
python -m pyflakes app.py db.py parsers pika.py tests
```
