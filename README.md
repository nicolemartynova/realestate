# Realty Telegram Bot

Рабочий MVP Telegram-бота с закрытой CRM-админкой для объектов недвижимости.

## Что уже есть

- закрытая админка по логину и паролю;
- SQLite-база объектов, медиа, подписчиков, просмотров, заявок и рассылок;
- добавление и редактирование объектов;
- загрузка фото/видео в объект;
- архивирование неактуальных объектов;
- Telegram polling-бот без webhook;
- приветствие и кнопка `Смотреть объекты`;
- выдача актуальных объектов пользователю;
- кнопки `Хочу узнать подробнее`, `Связаться`, `Смотреть еще`;
- сбор заявок через Telegram, телефон или WhatsApp;
- уведомление администратора о новых заявках;
- отправка объекта всем подписчикам из админки;
- отложенные рассылки;
- ежедневная отправка одного непросмотренного актуального объекта в окне 09:00-21:00.

## Поля объекта

Обязательные:

- категория;
- район;
- название здания;
- количество комнат;
- этаж;
- статус: свободно или в аренде;
- площадь;
- цена.

Необязательные:

- санузлы;
- парковка;
- меблировка;
- балкон;
- средняя цена рынка;
- distress;
- original price;
- дополнительное описание.

## Локальный запуск

```bash
cd tg_realty_bot
cp .env.example .env
```

Заполните `.env`, затем:

```bash
set -a
. ./.env
set +a
python3 app.py
```

Админка будет доступна на `http://127.0.0.1:8080`.

## Важно для медиа в Telegram

Фото и видео отправляются Telegram по публичным URL. Поэтому на VPS нужно указать:

```bash
PUBLIC_BASE_URL=https://your-domain.com
```

И настроить nginx так, чтобы домен проксировал админку на локальный порт приложения.

## Пример systemd

```ini
[Unit]
Description=Realty Telegram Bot
After=network.target

[Service]
WorkingDirectory=/opt/tg_realty_bot
EnvironmentFile=/opt/tg_realty_bot/.env
ExecStart=/usr/bin/python3 /opt/tg_realty_bot/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## Минимальный nginx proxy

```nginx
server {
    server_name your-domain.com;

    client_max_body_size 100M;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

TLS лучше подключить через `certbot`.
