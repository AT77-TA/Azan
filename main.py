import os
import json
import time
import hmac
import hashlib
import logging

import requests

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


# ============================================================
# НАСТРОЙКИ
# ============================================================

TUYA_ACCESS_ID = os.getenv("TUYA_ACCESS_ID")
TUYA_ACCESS_SECRET = os.getenv("TUYA_ACCESS_SECRET")
TUYA_DEVICE_ID = os.getenv("TUYA_DEVICE_ID")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TUYA_BASE_URL = "https://openapi.tuyaeu.com"

SCHEDULE_FILE = "schedule.json"

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


# ============================================================
# АЗАН
# ============================================================

# Через сколько минут после времени намаза включаем розетку
START_DELAY_MINUTES = 2

# Сколько минут работает азан
AZAN_DURATION_MINUTES = 6

# За сколько минут ДО запуска проверяем Tuya
TUYA_PRECHECK_MINUTES = 10


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_CHECK_INTERVAL_SECONDS = 3


# ============================================================
# НАМАЗЫ
# ============================================================

PRAYERS = [
    "Зухр",
    "Аср",
    "Магриб",
    "Иша"
]


# ============================================================
# СОСТОЯНИЕ
# ============================================================

schedule = {}

executed_events = set()

prechecked_events = set()

telegram_update_offset = None

last_telegram_check = None

azan_active = False
azan_prayer = None
azan_started_at = None
azan_stop_time = None


# ============================================================
# TUYA TOKEN CACHE
# ============================================================

tuya_access_token = None
tuya_token_expires_at = 0


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s"
)


def now_moscow():
    return datetime.now(MOSCOW_TZ)


def log(level, message):

    timestamp = now_moscow().strftime(
        "%d.%m.%Y %H:%M:%S"
    )

    logging.info(
        f"{timestamp} | {level} | {message}"
    )


# ============================================================
# TELEGRAM KEYBOARD
# ============================================================

def get_main_keyboard():

    return {
        "keyboard": [
            [
                {"text": "📊 Статус"},
                {"text": "📅 Расписание"}
            ],
            [
                {"text": "🟢 Включить"},
                {"text": "🔴 Выключить"}
            ],
            [
                {"text": "ℹ️ Помощь"}
            ]
        ],
        "resize_keyboard": True,
        "persistent": True
    }


# ============================================================
# TELEGRAM SEND
# ============================================================

def telegram_notify(message, show_keyboard=False):

    if not TELEGRAM_BOT_TOKEN:

        log(
            "ERROR",
            "❌ TELEGRAM_BOT_TOKEN не найден"
        )

        return False

    if not TELEGRAM_CHAT_ID:

        log(
            "ERROR",
            "❌ TELEGRAM_CHAT_ID не найден"
        )

        return False

    try:

        url = (
            f"https://api.telegram.org/bot"
            f"{TELEGRAM_BOT_TOKEN}/sendMessage"
        )

        payload = {
            "chat_id": str(TELEGRAM_CHAT_ID).strip(),
            "text": message
        }

        if show_keyboard:

            payload["reply_markup"] = (
                get_main_keyboard()
            )

        response = requests.post(
            url,
            json=payload,
            timeout=15
        )

        if response.status_code != 200:

            log(
                "ERROR",
                f"❌ Telegram ошибка: "
                f"{response.status_code} | "
                f"{response.text}"
            )

            return False

        data = response.json()

        if not data.get("ok"):

            log(
                "ERROR",
                f"❌ Telegram API ошибка: {data}"
            )

            return False

        return True

    except Exception as error:

        log(
            "ERROR",
            f"❌ Ошибка Telegram: {error}"
        )

        return False


# ============================================================
# TELEGRAM COMMANDS MENU
# ============================================================

def setup_telegram_commands():

    try:

        url = (
            f"https://api.telegram.org/bot"
            f"{TELEGRAM_BOT_TOKEN}/setMyCommands"
        )

        commands = {
            "commands": [
                {
                    "command": "start",
                    "description": "Запустить меню"
                },
                {
                    "command": "status",
                    "description": "Статус системы"
                },
                {
                    "command": "schedule",
                    "description": "Расписание азанов"
                },
                {
                    "command": "on",
                    "description": "Включить розетку"
                },
                {
                    "command": "off",
                    "description": "Выключить розетку"
                },
                {
                    "command": "help",
                    "description": "Помощь"
                }
            ]
        }

        response = requests.post(
            url,
            json=commands,
            timeout=15
        )

        response.raise_for_status()

        log(
            "INFO",
            "☰ Меню Telegram настроено"
        )

    except Exception as error:

        log(
            "ERROR",
            f"❌ Ошибка меню Telegram: {error}"
        )


# ============================================================
# TELEGRAM UPDATES
# ============================================================

def get_telegram_updates():

    global telegram_update_offset

    try:

        url = (
            f"https://api.telegram.org/bot"
            f"{TELEGRAM_BOT_TOKEN}/getUpdates"
        )

        params = {
            "timeout": 1
        }

        if telegram_update_offset is not None:

            params["offset"] = telegram_update_offset

        response = requests.get(
            url,
            params=params,
            timeout=10
        )

        response.raise_for_status()

        data = response.json()

        if not data.get("ok"):
            return []

        return data.get("result", [])

    except Exception as error:

        log(
            "ERROR",
            f"❌ Ошибка Telegram updates: {error}"
        )

        return []


# ============================================================
# HELP
# ============================================================

def send_help():

    message = (
        "ℹ️ УПРАВЛЕНИЕ СИСТЕМОЙ\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📊 Статус — состояние розетки\n"
        "📅 Расписание — расписание азанов\n"
        "🟢 Включить — ручное включение\n"
        "🔴 Выключить — ручное выключение\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 Автоматическая система азана"
    )

    telegram_notify(
        message,
        show_keyboard=True
    )


# ============================================================
# TELEGRAM COMMAND PROCESSING
# ============================================================

def process_telegram_commands():

    global telegram_update_offset
    global last_telegram_check

    now = now_moscow()

    if last_telegram_check:

        seconds_passed = (
            now - last_telegram_check
        ).total_seconds()

        if seconds_passed < TELEGRAM_CHECK_INTERVAL_SECONDS:
            return

    last_telegram_check = now

    updates = get_telegram_updates()

    for update in updates:

        update_id = update.get("update_id")

        if update_id is not None:

            telegram_update_offset = update_id + 1

        message = update.get("message")

        if not message:
            continue

        chat = message.get("chat", {})

        chat_id = str(
            chat.get("id")
        )

        text = message.get(
            "text",
            ""
        ).strip()

        if not text:
            continue

        # Защита от чужих пользователей
        if chat_id != str(TELEGRAM_CHAT_ID):

            log(
                "WARNING",
                f"⚠️ Чужой CHAT_ID: {chat_id}"
            )

            continue

        log(
            "INFO",
            f"🤖 Telegram: {text}"
        )

        # ----------------------------------------------------
        # START
        # ----------------------------------------------------

        if text == "/start":

            telegram_notify(
                "🕌 ШЕНДЖИЙСКАЯ МЕЧЕТЬ\n\n"
                "🤖 Панель управления азаном\n\n"
                "Выберите действие 👇",
                show_keyboard=True
            )

        # ----------------------------------------------------
        # STATUS
        # ----------------------------------------------------

        elif text in [
            "/status",
            "📊 Статус"
        ]:

            send_system_status()

        # ----------------------------------------------------
        # SCHEDULE
        # ----------------------------------------------------

        elif text in [
            "/schedule",
            "📅 Расписание"
        ]:

            send_schedule_to_telegram()

        # ----------------------------------------------------
        # ON
        # ----------------------------------------------------

        elif text in [
            "/on",
            "🟢 Включить"
        ]:

            try:

                set_socket(True)

                telegram_notify(
                    "🔌 ШЕНДЖИЙСКАЯ МЕЧЕТЬ\n\n"
                    "🟢 РОЗЕТКА ВКЛЮЧЕНА\n\n"
                    "👤 Управление через Telegram",
                    show_keyboard=True
                )

            except Exception as error:

                telegram_notify(
                    "❌ ОШИБКА ВКЛЮЧЕНИЯ\n\n"
                    f"{error}",
                    show_keyboard=True
                )

        # ----------------------------------------------------
        # OFF
        # ----------------------------------------------------

        elif text in [
            "/off",
            "🔴 Выключить"
        ]:

            try:

                set_socket(False)

                telegram_notify(
                    "🔌 ШЕНДЖИЙСКАЯ МЕЧЕТЬ\n\n"
                    "🔴 РОЗЕТКА ВЫКЛЮЧЕНА\n\n"
                    "👤 Управление через Telegram",
                    show_keyboard=True
                )

            except Exception as error:

                telegram_notify(
                    "❌ ОШИБКА ВЫКЛЮЧЕНИЯ\n\n"
                    f"{error}",
                    show_keyboard=True
                )

        # ----------------------------------------------------
        # HELP
        # ----------------------------------------------------

        elif text in [
            "/help",
            "ℹ️ Помощь"
        ]:

            send_help()

        else:

            telegram_notify(
                "❓ Неизвестная команда\n\n"
                "Используйте кнопки ниже 👇",
                show_keyboard=True
            )


# ============================================================
# SECRETS
# ============================================================

def check_secrets():

    missing = []

    if not TUYA_ACCESS_ID:
        missing.append("TUYA_ACCESS_ID")

    if not TUYA_ACCESS_SECRET:
        missing.append("TUYA_ACCESS_SECRET")

    if not TUYA_DEVICE_ID:
        missing.append("TUYA_DEVICE_ID")

    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")

    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:

        raise Exception(
            "Не найдены Secrets: "
            + ", ".join(missing)
        )

    log(
        "INFO",
        "✅ Все Secrets найдены"
    )


# ============================================================
# SCHEDULE JSON
# ============================================================

def load_schedule():

    global schedule

    if not os.path.exists(SCHEDULE_FILE):

        raise Exception(
            f"Файл {SCHEDULE_FILE} не найден"
        )

    with open(
        SCHEDULE_FILE,
        "r",
        encoding="utf-8"
    ) as file:

        schedule = json.load(file)

    if not schedule:

        raise Exception(
            "schedule.json пустой"
        )

    log(
        "INFO",
        "📅 Расписание загружено из JSON"
    )


# ============================================================
# TODAY EVENTS
# ============================================================

def get_today_events():

    today = now_moscow().date()

    date_key = today.strftime(
        "%Y-%m-%d"
    )

    today_schedule = schedule.get(
        date_key,
        {}
    )

    events = {}

    for prayer in PRAYERS:

        prayer_time = today_schedule.get(
            prayer
        )

        if not prayer_time:
            continue

        hour, minute = map(
            int,
            prayer_time.split(":")
        )

        prayer_datetime = datetime(
            today.year,
            today.month,
            today.day,
            hour,
            minute,
            tzinfo=MOSCOW_TZ
        )

        event_time = (
            prayer_datetime
            + timedelta(
                minutes=START_DELAY_MINUTES
            )
        )

        events[prayer] = event_time

    return events


# ============================================================
# TELEGRAM SCHEDULE
# ============================================================

def send_schedule_to_telegram():

    now = now_moscow()

    events = get_today_events()

    message = (
        "🕌 ШЕНДЖИЙСКАЯ МЕЧЕТЬ\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 {now.strftime('%d.%m.%Y')}\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    for prayer in PRAYERS:

        event_time = events.get(
            prayer
        )

        if event_time:

            message += (
                f"🕌 {prayer} — "
                f"{event_time.strftime('%H:%M')}\n"
            )

    message += (
        "\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🔊 Длительность: "
        f"{AZAN_DURATION_MINUTES} мин.\n"
        f"⏱ Задержка: +"
        f"{START_DELAY_MINUTES} мин."
    )

    telegram_notify(
        message,
        show_keyboard=True
    )


# ============================================================
# TUYA SHA256
# ============================================================

def sha256(data):

    return hashlib.sha256(
        data.encode("utf-8")
    ).hexdigest()


# ============================================================
# TUYA SIGNATURE
# ============================================================

def create_signature(
    method,
    path,
    token="",
    body=""
):

    timestamp = str(
        int(time.time() * 1000)
    )

    content_hash = sha256(
        body
    )

    string_to_sign = (
        method
        + "\n"
        + content_hash
        + "\n"
        + "\n"
        + path
    )

    sign_string = (
        TUYA_ACCESS_ID
        + token
        + timestamp
        + string_to_sign
    )

    sign = hmac.new(
        TUYA_ACCESS_SECRET.encode("utf-8"),
        sign_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest().upper()

    return timestamp, sign


# ============================================================
# TUYA TOKEN
# ============================================================

def get_tuya_token():

    global tuya_access_token
    global tuya_token_expires_at

    now_timestamp = time.time()

    # --------------------------------------------------------
    # Используем существующий token
    #
    # Обновляем его только если осталось меньше 5 минут.
    # --------------------------------------------------------

    if (
        tuya_access_token
        and now_timestamp
        < tuya_token_expires_at - 300
    ):

        return tuya_access_token

    log(
        "INFO",
        "🔑 Получаем новый Tuya token"
    )

    path = (
        "/v1.0/token?grant_type=1"
    )

    timestamp, sign = create_signature(
        "GET",
        path
    )

    headers = {
        "client_id": TUYA_ACCESS_ID,
        "sign": sign,
        "t": timestamp,
        "sign_method": "HMAC-SHA256"
    }

    response = requests.get(
        TUYA_BASE_URL + path,
        headers=headers,
        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    if not data.get("success"):

        raise Exception(
            f"Tuya token error: {data}"
        )

    result = data.get(
        "result",
        {}
    )

    token = result.get(
        "access_token"
    )

    expire_time = result.get(
        "expire_time",
        7200
    )

    if not token:

        raise Exception(
            "Tuya token отсутствует"
        )

    tuya_access_token = token

    tuya_token_expires_at = (
        now_timestamp
        + int(expire_time)
    )

    log(
        "INFO",
        "🔑 Tuya token сохранён в памяти"
    )

    return tuya_access_token


# ============================================================
# TUYA REQUEST
# ============================================================

def tuya_request(
    method,
    path,
    body=""
):

    token = get_tuya_token()

    timestamp, sign = create_signature(
        method,
        path,
        token,
        body
    )

    headers = {
        "client_id": TUYA_ACCESS_ID,
        "access_token": token,
        "sign": sign,
        "t": timestamp,
        "sign_method": "HMAC-SHA256",
        "Content-Type": "application/json"
    }

    if method == "GET":

        response = requests.get(
            TUYA_BASE_URL + path,
            headers=headers,
            timeout=30
        )

    else:

        response = requests.post(
            TUYA_BASE_URL + path,
            headers=headers,
            data=body,
            timeout=30
        )

    response.raise_for_status()

    data = response.json()

    if not data.get("success"):

        raise Exception(
            f"Tuya API error: {data}"
        )

    return data


# ============================================================
# GET SOCKET STATE
# ============================================================

def get_socket_state():

    path = (
        f"/v1.0/iot-03/devices/"
        f"{TUYA_DEVICE_ID}/status"
    )

    data = tuya_request(
        "GET",
        path
    )

    for item in data.get(
        "result",
        []
    ):

        if item.get("code") == "switch_1":

            return item.get("value")

    raise Exception(
        "switch_1 не найден"
    )


# ============================================================
# SET SOCKET
# ============================================================

def set_socket(state):

    path = (
        f"/v1.0/iot-03/devices/"
        f"{TUYA_DEVICE_ID}/commands"
    )

    body_dict = {
        "commands": [
            {
                "code": "switch_1",
                "value": state
            }
        ]
    }

    body = json.dumps(
        body_dict,
        separators=(",", ":")
    )

    tuya_request(
        "POST",
        path,
        body
    )

    log(
        "INFO",
        "🔌 Розетка "
        + (
            "ВКЛЮЧЕНА"
            if state
            else "ВЫКЛЮЧЕНА"
        )
    )


# ============================================================
# ПРОВЕРКА TUYA ПЕРЕД АЗАНОМ
# ============================================================

def precheck_tuya(prayer, event_time):

    event_id = (
        f"{now_moscow().date()}_{prayer}"
    )

    if event_id in prechecked_events:
        return

    now = now_moscow()

    precheck_time = (
        event_time
        - timedelta(
            minutes=TUYA_PRECHECK_MINUTES
        )
    )

    if now < precheck_time:
        return

    if now >= event_time:
        return

    log(
        "INFO",
        f"🔍 Предварительная проверка Tuya: "
        f"{prayer}"
    )

    try:

        state = get_socket_state()

        log(
            "INFO",
            "🔌 Tuya доступна. "
            f"Текущее состояние: "
            + (
                "ВКЛ"
                if state
                else "ВЫКЛ"
            )
        )

        telegram_notify(
            f"🟢 ПРОВЕРКА ПЕРЕД АЗАНОМ\n\n"
            f"🕌 {prayer}\n"
            f"🕐 Запуск: "
            f"{event_time.strftime('%H:%M')}\n"
            "🔌 Розетка доступна\n"
            "✅ Система готова"
        )

        prechecked_events.add(
            event_id
        )

    except Exception as error:

        log(
            "ERROR",
            f"❌ Предварительная проверка "
            f"Tuya не удалась: {error}"
        )

        telegram_notify(
            f"⚠️ ПРОБЛЕМА ПЕРЕД АЗАНОМ\n\n"
            f"🕌 {prayer}\n"
            f"❌ Розетка недоступна\n\n"
            f"{error}"
        )

        # Помечаем как проверенное,
        # чтобы не спамить запросами
        prechecked_events.add(
            event_id
        )


# ============================================================
# ЗАПУСК АЗАНА
# ============================================================

def start_azan(prayer):

    global azan_active
    global azan_prayer
    global azan_started_at
    global azan_stop_time

    now = now_moscow()

    event_id = (
        f"{now.date()}_{prayer}"
    )

    if event_id in executed_events:
        return

    if azan_active:
        return

    executed_events.add(
        event_id
    )

    log(
        "INFO",
        f"🕌 ЗАПУСК АЗАНА: {prayer}"
    )

    try:

        # Включаем
        set_socket(True)

        # ----------------------------------------------------
        # СРАЗУ ПОСЛЕ ВКЛЮЧЕНИЯ ПРОВЕРЯЕМ СОСТОЯНИЕ
        # ----------------------------------------------------

        actual_state = get_socket_state()

        if actual_state is not True:

            raise Exception(
                "Розетка не подтвердила "
                "включение"
            )

        azan_active = True

        azan_prayer = prayer

        azan_started_at = now

        azan_stop_time = (
            now
            + timedelta(
                minutes=AZAN_DURATION_MINUTES
            )
        )

        telegram_notify(
            f"🕌 АЗАН НАЧАЛСЯ\n\n"
            f"🕌 {prayer}\n"
            f"🕐 {now.strftime('%H:%M:%S')}\n"
            "🔌 Розетка включена\n"
            "✅ Включение подтверждено\n"
            f"⏳ Автовыключение через "
            f"{AZAN_DURATION_MINUTES} мин."
        )

    except Exception as error:

        log(
            "ERROR",
            f"❌ Ошибка запуска азана: {error}"
        )

        telegram_notify(
            f"⚠️ ОШИБКА АЗАНА\n\n"
            f"🕌 {prayer}\n"
            f"❌ {error}"
        )


# ============================================================
# ОСТАНОВКА АЗАНА
# ============================================================

def check_azan_stop():

    global azan_active
    global azan_prayer
    global azan_started_at
    global azan_stop_time

    if not azan_active:
        return

    if not azan_stop_time:
        return

    now = now_moscow()

    if now < azan_stop_time:
        return

    prayer = azan_prayer

    try:

        log(
            "INFO",
            f"⏹ ЗАВЕРШЕНИЕ АЗАНА: {prayer}"
        )

        set_socket(False)

        # Подтверждаем выключение
        actual_state = get_socket_state()

        if actual_state is not False:

            raise Exception(
                "Розетка не подтвердила "
                "выключение"
            )

        telegram_notify(
            f"⏹ АЗАН ЗАВЕРШЁН\n\n"
            f"🕌 {prayer}\n"
            "🔴 Розетка автоматически "
            "выключена\n"
            "✅ Выключение подтверждено"
        )

    except Exception as error:

        log(
            "ERROR",
            f"❌ Ошибка выключения: {error}"
        )

        telegram_notify(
            f"⚠️ ОШИБКА ВЫКЛЮЧЕНИЯ\n\n"
            f"🕌 {prayer}\n"
            f"❌ {error}"
        )

    finally:

        azan_active = False
        azan_prayer = None
        azan_started_at = None
        azan_stop_time = None


# ============================================================
# SYSTEM STATUS
# ============================================================

def send_system_status():

    now = now_moscow()

    try:

        state = get_socket_state()

        socket_text = (
            "🟢 ВКЛЮЧЕНА"
            if state
            else "🔴 ВЫКЛЮЧЕНА"
        )

        connection_text = "🟢 ONLINE"

    except Exception as error:

        connection_text = "🔴 OFFLINE"
        socket_text = "⚠️ НЕДОСТУПНО"

        log(
            "ERROR",
            f"❌ Ошибка статуса: {error}"
        )

    events = get_today_events()

    message = (
        "🕌 ШЕНДЖИЙСКАЯ МЕЧЕТЬ\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🤖 Система: 🟢 ONLINE\n"
        f"🔌 Tuya: {connection_text}\n"
        f"⚡ Розетка: {socket_text}\n"
    )

    if azan_active:

        remaining = (
            azan_stop_time - now
        )

        seconds = max(
            0,
            int(
                remaining.total_seconds()
            )
        )

        minutes = seconds // 60
        secs = seconds % 60

        message += (
            "\n🔊 АЗАН СЕЙЧАС ИДЁТ\n"
            f"🕌 {azan_prayer}\n"
            f"⏳ Осталось: "
            f"{minutes} мин. {secs} сек.\n"
        )

    message += "\n📅 СЕГОДНЯ:\n"

    for prayer in PRAYERS:

        event_time = events.get(
            prayer
        )

        if event_time:

            message += (
                f"🕌 {prayer} — "
                f"{event_time.strftime('%H:%M')}\n"
            )

    message += (
        "\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 Сейчас: "
        f"{now.strftime('%H:%M:%S')}"
    )

    telegram_notify(
        message,
        show_keyboard=True
    )


# ============================================================
# ПРОВЕРКА РАСПИСАНИЯ
# ============================================================

def check_schedule():

    events = get_today_events()

    if not events:

        log(
            "WARNING",
            "⚠️ На сегодня расписание не найдено"
        )

        return

    now = now_moscow()

    for prayer, event_time in events.items():

        # -----------------------------------------------
        # Проверяем Tuya за 10 минут до запуска
        # -----------------------------------------------

        precheck_tuya(
            prayer,
            event_time
        )

        # -----------------------------------------------
        # Проверяем время запуска
        # -----------------------------------------------

        event_id = (
            f"{now.date()}_{prayer}"
        )

        if event_id in executed_events:
            continue

        if now >= event_time:

            difference = (
                now - event_time
            ).total_seconds()

            # Запуск только в пределах первых 120 секунд
            if 0 <= difference <= 120:

                start_azan(
                    prayer
                )

            elif difference > 120:

                executed_events.add(
                    event_id
                )

                log(
                    "INFO",
                    f"⏭️ {prayer} уже прошёл"
                )


# ============================================================
# НОВЫЙ ДЕНЬ
# ============================================================

def cleanup_old_events():

    today = now_moscow().date()

    prefix = f"{today}_"

    old_events = {
        event
        for event in executed_events
        if not event.startswith(prefix)
    }

    executed_events.difference_update(
        old_events
    )

    old_prechecks = {
        event
        for event in prechecked_events
        if not event.startswith(prefix)
    }

    prechecked_events.difference_update(
        old_prechecks
    )


# ============================================================
# MAIN
# ============================================================

def run_production_mode():

    log(
        "INFO",
        "=" * 60
    )

    log(
        "INFO",
        "🕌 АЗАН — ШЕНДЖИЙСКАЯ МЕЧЕТЬ"
    )

    log(
        "INFO",
        "🤖 СИСТЕМА ЗАПУЩЕНА"
    )

    log(
        "INFO",
        "=" * 60
    )

    check_secrets()

    load_schedule()

    setup_telegram_commands()

    telegram_notify(
        "🟢 ШЕНДЖИЙСКАЯ МЕЧЕТЬ\n\n"
        "🤖 Система автоматического азана "
        "запущена\n"
        "📅 Расписание: JSON\n"
        "⏱ Задержка: +2 минуты\n"
        "🔊 Длительность: 6 минут\n"
        "🔍 Проверка Tuya: за 10 минут\n"
        "📡 Telegram активен",
        show_keyboard=True
    )

    send_schedule_to_telegram()

    while True:

        try:

            # Telegram проверяем регулярно.
            # Это НЕ запрос к Tuya.
            process_telegram_commands()

            # Проверка расписания.
            # Tuya здесь вызывается только:
            # - за 10 минут до азана
            # - во время запуска
            # - при завершении
            check_schedule()

            # Проверка окончания азана
            check_azan_stop()

            # Очистка старых событий
            cleanup_old_events()

            time.sleep(1)

        except KeyboardInterrupt:

            log(
                "INFO",
                "🛑 Система остановлена вручную"
            )

            break

        except Exception as error:

            log(
                "ERROR",
                f"❌ Ошибка системы: {error}"
            )

            time.sleep(30)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    run_production_mode()
