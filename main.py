import os
import json
import time
import hmac
import hashlib
import requests
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


# =========================
# НАСТРОЙКИ
# =========================

TIMEZONE = ZoneInfo("Europe/Moscow")

TUYA_BASE_URL = "https://openapi.tuyaeu.com"

TUYA_ACCESS_ID = os.getenv("TUYA_ACCESS_ID")
TUYA_ACCESS_SECRET = os.getenv("TUYA_ACCESS_SECRET")
TUYA_DEVICE_ID = os.getenv("TUYA_DEVICE_ID")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SCHEDULE_FILE = "schedule.json"
SOCKET_SWITCH_CODE = "switch_1"

# Проверяем локальное время каждые 5 секунд.
# Это НЕ запрос к Tuya.
CHECK_INTERVAL_SECONDS = 5
SOCKET_STATUS_CHECK_SECONDS = 3600  # В обычное время проверка состояния раз в час.
AZAN_STATUS_CHECK_SECONDS = 10       # Во время азана проверяем состояние каждые 10 секунд.
POST_AZAN_STATUS_CHECK_SECONDS = 20  # После выключения проверяем состояние каждые 20 секунд.
POST_AZAN_VERIFY_SECONDS = 60        # Продолжаем контроль ещё 1 минуту после команды выключения.
SOCKET_TOKEN_REFRESH_SECONDS = 3600  # Кэш токена; при ошибке 1010 он обновится раньше.

# За сколько минут до азана заранее получаем Tuya token.
PREPARE_MINUTES = 5

# Через сколько минут после включения выключаем розетку.
AZAN_DURATION_MINUTES = 6

# +2 минуты к времени из расписания.
START_DELAY_MINUTES = 2

# Названия молитв, которые используем.
PRAYERS = [
    "Fajr",
    "Dhuhr",
    "Asr",
    "Maghrib",
    "Isha",
]


telegram_update_offset = None
last_telegram_check = None
TELEGRAM_CHECK_INTERVAL_SECONDS = 3
prepared_token = None
prepared_prayer_key = None

# Мониторинг состояния розетки
last_socket_state = None
last_socket_check = None
socket_is_online = None
socket_offline_notified = False
socket_monitor_token = None
socket_monitor_token_time = None
# Время окончания текущего окна мониторинга азана (Unix timestamp).
active_azan_monitor_until = 0
# Окончание дополнительного контроля после команды автоматического выключения.
post_azan_monitor_until = 0

PRAYER_NAMES_RU = {"Fajr":"Фаджр","Dhuhr":"Зухр","Asr":"Аср","Maghrib":"Магриб","Isha":"Иша"}


# =========================
# ПРОВЕРКА НАСТРОЕК
# =========================

def check_environment():
    required = {
        "TUYA_ACCESS_ID": TUYA_ACCESS_ID,
        "TUYA_ACCESS_SECRET": TUYA_ACCESS_SECRET,
        "TUYA_DEVICE_ID": TUYA_DEVICE_ID,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    }

    missing = [name for name, value in required.items() if not value]

    if missing:
        raise RuntimeError(
            "Не найдены переменные окружения: "
            + ", ".join(missing)
        )

    print("✅ Все необходимые Secrets найдены")


# =========================
# JSON РАСПИСАНИЕ
# =========================

def load_schedule():
    if not os.path.exists(SCHEDULE_FILE):
        raise FileNotFoundError(
            f"Файл {SCHEDULE_FILE} не найден"
        )

    with open(SCHEDULE_FILE, "r", encoding="utf-8") as file:
        data = json.load(file)

    return data


def get_today_schedule():
    data = load_schedule()

    today = datetime.now(TIMEZONE).date()
    date_string = today.strftime("%Y-%m-%d")

    # Вариант:
    # {
    #   "2026-10-04": {
    #       "Fajr": "05:12",
    #       ...
    #   }
    # }

    if isinstance(data, dict):
        if date_string in data:
            return data[date_string]

        # Если JSON имеет структуру {"dates": {...}}
        if "dates" in data and date_string in data["dates"]:
            return data["dates"][date_string]

    return {}
    

# =========================
# ВРЕМЯ АЗАНА
# =========================

def get_prayer_datetime(prayer_time):
    now = datetime.now(TIMEZONE)

    hour, minute = map(int, prayer_time.split(":"))

    return now.replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )


def get_next_prayer():
    schedule = get_today_schedule()

    now = datetime.now(TIMEZONE)

    candidates = []

    for prayer in PRAYERS:
        if prayer not in schedule:
            continue

        prayer_time = schedule[prayer]

        if not prayer_time:
            continue

        prayer_dt = get_prayer_datetime(prayer_time)

        # +2 минуты — фактическое время запуска.
        start_dt = prayer_dt + timedelta(
            minutes=START_DELAY_MINUTES
        )

        if start_dt > now:
            candidates.append(
                (start_dt, prayer, prayer_time)
            )

    if not candidates:
        return None

    return min(candidates, key=lambda item: item[0])


# =========================
# TUYA
# =========================

def get_tuya_token():
    """
    Получаем token Tuya.

    Этот запрос НЕ вызывается каждые 5 секунд.
    Мы вызываем его примерно за 5 минут
    до нужного времени.
    """

    timestamp = str(int(time.time() * 1000))

    method = "GET"
    path = "/v1.0/token?grant_type=1"

    string_to_sign = method + "\n" + hashlib.sha256(
        b""
    ).hexdigest() + "\n\n" + path

    sign_message = (
        TUYA_ACCESS_ID
        + timestamp
        + string_to_sign
    )

    sign = hmac.new(
        TUYA_ACCESS_SECRET.encode(),
        sign_message.encode(),
        hashlib.sha256,
    ).hexdigest().upper()

    headers = {
        "client_id": TUYA_ACCESS_ID,
        "sign": sign,
        "t": timestamp,
        "sign_method": "HMAC-SHA256",
    }

    response = requests.get(
        TUYA_BASE_URL + path,
        headers=headers,
        timeout=15,
    )

    response.raise_for_status()

    data = response.json()

    if not data.get("success"):
        raise RuntimeError(
            f"Ошибка получения Tuya token: {data}"
        )

    token = data["result"]["access_token"]

    print("🔑 Tuya token получен")

    return token


def tuya_request(token, method, path, body="", retry_on_invalid_token=True):
    """Отправляет запрос в Tuya; при ошибке 1010 обновляет токен и повторяет один раз."""
    global socket_monitor_token, socket_monitor_token_time, prepared_token

    # Время и подпись нужно пересоздавать для каждого запроса.
    timestamp = str(int(time.time() * 1000))
    body_hash = hashlib.sha256(body.encode()).hexdigest()
    string_to_sign = method + "\n" + body_hash + "\n\n" + path
    sign_message = TUYA_ACCESS_ID + token + timestamp + string_to_sign
    sign = hmac.new(
        TUYA_ACCESS_SECRET.encode(), sign_message.encode(), hashlib.sha256
    ).hexdigest().upper()

    headers = {
        "client_id": TUYA_ACCESS_ID,
        "access_token": token,
        "sign": sign,
        "t": timestamp,
        "sign_method": "HMAC-SHA256",
        "Content-Type": "application/json",
    }
    url = TUYA_BASE_URL + path

    if method == "GET":
        response = requests.get(url, headers=headers, timeout=15)
    else:
        response = requests.post(url, headers=headers, data=body, timeout=15)

    response.raise_for_status()
    data = response.json()

    # 1010 означает, что переданный токен больше не действителен.
    # Получаем новый и повторяем исходный запрос только один раз,
    # чтобы избежать бесконечного цикла и лишних обращений к Tuya.
    if not data.get("success"):
        if data.get("code") == 1010 and retry_on_invalid_token:
            print("⚠️ Tuya отклонила токен (1010). Получаю новый и повторяю запрос один раз.")
            fresh_token = get_tuya_token()
            socket_monitor_token = fresh_token
            socket_monitor_token_time = time.time()
            prepared_token = fresh_token
            return tuya_request(
                fresh_token, method, path, body, retry_on_invalid_token=False
            )
        raise RuntimeError(f"Tuya API error: {data}")

    return data

def get_socket_state(token):
    path = (
        f"/v1.0/devices/"
        f"{TUYA_DEVICE_ID}/status"
    )

    data = tuya_request(
        token,
        "GET",
        path,
    )

    for item in data["result"]:
        if item["code"] == SOCKET_SWITCH_CODE:
            return bool(item["value"])

        if item["code"] in (
            "switch",
            "switch_led",
        ):
            return bool(item["value"])

    raise RuntimeError(
        "Не удалось найти состояние розетки"
    )


def set_socket(token, state):
    # Отправляет команду включения/выключения в Tuya.
    # ВАЖНО: не меняем здесь last_socket_state. Эта переменная хранит
    # последнее ФАКТИЧЕСКИ ПРОЧИТАННОЕ состояние розетки, а не то,
    # какую команду мы попросили выполнить. Иначе мониторинг не заметит
    # реальное изменение состояния после нашей команды.
    path = (
        f"/v1.0/devices/"
        f"{TUYA_DEVICE_ID}/commands"
    )

    body = json.dumps(
        {
            "commands": [
                {
                    "code": SOCKET_SWITCH_CODE,
                    "value": state,
                }
            ]
        },
        separators=(",", ":"),
    )

    data = tuya_request(
        token,
        "POST",
        path,
        body,
    )

    print(
        f"🔌 Розетка: "
        f"{'ВКЛ' if state else 'ВЫКЛ'}"
    )

    return data


# =========================
# TELEGRAM
# =========================

def telegram_send(message):
    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=15,
        )

        if not response.ok:
            print(
                "⚠️ Telegram error:",
                response.text,
            )

    except Exception as error:
        print(
            "⚠️ Ошибка Telegram:",
            error,
        )


# =========================
# TELEGRAM
# =========================

def telegram_notify(message, show_keyboard=False):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": str(TELEGRAM_CHAT_ID).strip(), "text": message}
    if show_keyboard:
        payload["reply_markup"] = {"keyboard":[[{"text":"📊 Статус"},{"text":"📅 Расписание"}],[{"text":"🟢 Включить"},{"text":"🔴 Выключить"}],[{"text":"ℹ️ Помощь"}]],"resize_keyboard":True,"persistent":True}
    try:
        response = requests.post(url, json=payload, timeout=15)
        data = response.json()
        if not response.ok or not data.get("ok"):
            print(f"⚠️ Telegram error: {response.text}")
            return False
        return True
    except Exception as error:
        print(f"⚠️ Ошибка Telegram: {error}")
        return False


def setup_telegram_commands():
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands"
    commands = {"commands":[{"command":"start","description":"Запустить меню"},{"command":"status","description":"Статус системы"},{"command":"schedule","description":"Расписание азанов"},{"command":"on","description":"Включить розетку"},{"command":"off","description":"Выключить розетку"},{"command":"help","description":"Помощь"}]}
    try:
        response = requests.post(url, json=commands, timeout=15)
        response.raise_for_status()
        print("☰ Меню Telegram настроено")
    except Exception as error:
        print(f"⚠️ Ошибка настройки меню Telegram: {error}")


def get_telegram_updates():
    global telegram_update_offset
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 1}
    if telegram_update_offset is not None:
        params["offset"] = telegram_update_offset
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data.get("result", []) if data.get("ok") else []
    except Exception as error:
        print(f"⚠️ Ошибка получения Telegram команд: {error}")
        return []


def get_today_events():
    schedule = get_today_schedule()
    events = {}

    for prayer in PRAYERS:
        prayer_time = schedule.get(prayer)
        if not prayer_time:
            continue

        prayer_dt = get_prayer_datetime(prayer_time)
        events[prayer] = prayer_dt + timedelta(minutes=START_DELAY_MINUTES)

    return events


def send_schedule_to_telegram():
    schedule = get_today_schedule()
    now = datetime.now(TIMEZONE)
    message = ("🕌 ШЕНДЖИЙСКАЯ МЕЧЕТЬ\n" "━━━━━━━━━━━━━━━━━━━━\n" f"📅 РАСПИСАНИЕ НА {now.strftime('%d.%m.%Y')}\n" "━━━━━━━━━━━━━━━━━━━━\n\n")
    for prayer in PRAYERS:
        prayer_time = schedule.get(prayer)
        if not prayer_time:
            continue
        start_dt = get_prayer_datetime(prayer_time) + timedelta(minutes=START_DELAY_MINUTES)
        message += f"🕌 {PRAYER_NAMES_RU.get(prayer, prayer)} — 🔊 {start_dt.strftime('%H:%M')}\n"
    message += f"\n━━━━━━━━━━━━━━━━━━━━\n🔊 Длительность: {AZAN_DURATION_MINUTES} мин.\n⏱ Запуск: +{START_DELAY_MINUTES} мин. от расписания\n🌍 Время: Москва"
    telegram_notify(message, show_keyboard=True)


def get_socket_monitor_token():
    global socket_monitor_token, socket_monitor_token_time

    now = time.time()

    if (
        socket_monitor_token is not None
        and socket_monitor_token_time is not None
        and now - socket_monitor_token_time < SOCKET_TOKEN_REFRESH_SECONDS
    ):
        return socket_monitor_token

    socket_monitor_token = get_tuya_token()
    socket_monitor_token_time = now
    return socket_monitor_token


def get_manual_tuya_token():
    return prepared_token if prepared_token is not None else get_socket_monitor_token()


def check_socket_manual_changes():
    """Обычно проверяет розетку раз в час; во время азана — раз в 10 секунд."""
    global last_socket_state, last_socket_check
    global socket_is_online, socket_offline_notified

    now = datetime.now(TIMEZONE)
    now_timestamp = time.time()
    azan_monitoring = now_timestamp < active_azan_monitor_until
    post_azan_monitoring = now_timestamp < post_azan_monitor_until

    # Приоритет интервалов:
    # 1) во время азана — 10 секунд;
    # 2) минуту после команды выключения — 20 секунд;
    # 3) всё остальное время — один раз в час.
    if azan_monitoring:
        required_interval = AZAN_STATUS_CHECK_SECONDS
    elif post_azan_monitoring:
        required_interval = POST_AZAN_STATUS_CHECK_SECONDS
    else:
        required_interval = SOCKET_STATUS_CHECK_SECONDS

    # Если интервал ещё не прошёл, не отправляем запрос в Tuya.
    if last_socket_check is not None:
        seconds_passed = (now - last_socket_check).total_seconds()
        if seconds_passed < required_interval:
            return
    last_socket_check = now

    try:
        current_state = get_socket_state(get_socket_monitor_token())

        if socket_is_online is False:
            telegram_notify(
                "🟢 РОЗЕТКА ONLINE\n\n"
                "🕌 Шенджийская мечеть\n"
                "🔌 Связь восстановлена"
            )
        socket_is_online = True
        socket_offline_notified = False

        # Первый опрос после старта только сохраняет состояние.
        if last_socket_state is None:
            last_socket_state = current_state
            print("🔍 Начальное состояние розетки: " + ("ВКЛ" if current_state else "ВЫКЛ"))
            return

        # Состояние не менялось — уведомление не требуется.
        if current_state == last_socket_state:
            return

        old_state = last_socket_state
        last_socket_state = current_state
        print(f"🔄 Изменение состояния розетки: {old_state} → {current_state}")

        # Вне азана состояние проверяем только для доступности устройства.
        # Во время азана уведомляем об изменениях. В течение минуты после
        # команды выключения тоже уведомляем, если состояние не ожидаемое.
        if azan_monitoring or post_azan_monitoring:
            if post_azan_monitoring:
                heading = "🔎 ИЗМЕНЕНИЕ СОСТОЯНИЯ ПОСЛЕ АЗАНА"
            else:
                heading = "🔔 СОСТОЯНИЕ РОЗЕТКИ ИЗМЕНИЛОСЬ ВО ВРЕМЯ АЗАНА"
            if current_state:
                telegram_notify(
                    f"{heading}\n\n"
                    "🟢 Розетка включена\n"
                    f"🕐 {now.strftime('%H:%M:%S')}"
                )
            else:
                telegram_notify(
                    f"{heading}\n\n"
                    "🔴 Розетка выключена\n"
                    f"🕐 {now.strftime('%H:%M:%S')}"
                )

    except Exception as error:
        socket_is_online = False
        print(f"❌ Розетка недоступна: {error}")
        if not socket_offline_notified:
            telegram_notify(
                "⚠️ РОЗЕТКА OFFLINE\n\n"
                "🕌 Шенджийская мечеть\n\n"
                "Не удаётся получить состояние устройства."
            )
            socket_offline_notified = True

def send_system_status():
    now = datetime.now(TIMEZONE)
    socket_status, socket_power = "🔴 OFFLINE", "⚠️ НЕДОСТУПНО"
    try:
        state = get_socket_state(get_manual_tuya_token())
        socket_status = "🟢 ONLINE"
        socket_power = "🟢 ВКЛЮЧЕНА" if state else "🔴 ВЫКЛЮЧЕНА"
    except Exception as error:
        print(f"⚠️ Ошибка статуса Tuya: {error}")
    try:
        next_prayer = get_next_prayer()
    except Exception:
        next_prayer = None
    message = ("🕌 ШЕНДЖИЙСКАЯ МЕЧЕТЬ\n━━━━━━━━━━━━━━━━━━━━\n\n" f"🤖 Сервер: 🟢 ONLINE\n🔌 Розетка: {socket_status}\n⚡ Состояние: {socket_power}\n")
    if next_prayer:
        start_dt, prayer_name, _ = next_prayer
        message += f"\n⏭ Следующий азан: {PRAYER_NAMES_RU.get(prayer_name, prayer_name)}\n🕐 Время: {start_dt.strftime('%H:%M')}\n"
    else:
        message += "\n🌙 На сегодня азанов больше нет\n"
    message += f"\n🕰 Сейчас: {now.strftime('%H:%M:%S')}"
    telegram_notify(message, show_keyboard=True)


def send_help():
    telegram_notify("ℹ️ УПРАВЛЕНИЕ СИСТЕМОЙ\n━━━━━━━━━━━━━━━━━━━━\n\n📊 Статус — состояние сервера и розетки\n📅 Расписание — расписание азанов\n🟢 Включить — включить розетку вручную\n🔴 Выключить — выключить розетку вручную\nℹ️ Помощь — эта справка\n\n🤖 Система работает автоматически 24/7", show_keyboard=True)


def process_telegram_commands():
    global telegram_update_offset, last_telegram_check, prepared_token
    now = datetime.now(TIMEZONE)
    if last_telegram_check is not None and (now - last_telegram_check).total_seconds() < TELEGRAM_CHECK_INTERVAL_SECONDS:
        return
    last_telegram_check = now
    for update in get_telegram_updates():
        update_id = update.get("update_id")
        if update_id is not None:
            telegram_update_offset = update_id + 1
        message = update.get("message") or {}
        chat_id = str(message.get("chat", {}).get("id"))
        text = message.get("text", "").strip()
        if not text or chat_id != str(TELEGRAM_CHAT_ID).strip():
            continue
        try:
            if text == "/start":
                telegram_notify("🕌 ШЕНДЖИЙСКАЯ МЕЧЕТЬ\n\n🤖 Панель управления системой азана\n\nВыберите действие 👇", show_keyboard=True)
                send_schedule_to_telegram()
            elif text in ("/status", "📊 Статус"):
                send_system_status()
            elif text in ("/schedule", "📅 Расписание"):
                send_schedule_to_telegram()
            elif text in ("/on", "🟢 Включить"):
                token = get_manual_tuya_token(); set_socket(token, True); prepared_token = token
                telegram_notify("🔌 ШЕНДЖИЙСКАЯ МЕЧЕТЬ\n\n🟢 РОЗЕТКА ВКЛЮЧЕНА\n\n👤 Управление через Telegram", show_keyboard=True)
            elif text in ("/off", "🔴 Выключить"):
                token = get_manual_tuya_token(); set_socket(token, False); prepared_token = token
                telegram_notify("🔌 ШЕНДЖИЙСКАЯ МЕЧЕТЬ\n\n🔴 РОЗЕТКА ВЫКЛЮЧЕНА\n\n👤 Управление через Telegram", show_keyboard=True)
            elif text in ("/help", "ℹ️ Помощь"):
                send_help()
            else:
                telegram_notify("❓ Неизвестная команда\n\nИспользуйте кнопки ниже 👇", show_keyboard=True)
        except Exception as error:
            telegram_notify(f"❌ Ошибка команды Telegram\n\n{error}", show_keyboard=True)


# =========================
# АЗАН
# =========================

def run_azan(token, prayer_name, prayer_time):
    global active_azan_monitor_until, post_azan_monitor_until, last_socket_state, last_socket_check
    try:
        # Во время азана включаем частый мониторинг на все 6 минут.
        active_azan_monitor_until = max(
            active_azan_monitor_until,
            time.time() + AZAN_DURATION_MINUTES * 60,
        )
        # Сбрасываем время последней проверки, чтобы мониторинг сразу
        # переключился с часового режима на проверку каждые 10 секунд.
        last_socket_check = None
        current_state = get_socket_state(token)
        if not current_state:
            set_socket(token, True)
        telegram_notify(f"🕌 {PRAYER_NAMES_RU.get(prayer_name, prayer_name)}\nАзан: {prayer_time} (+{START_DELAY_MINUTES} мин.)\nРозетка включена АВТОМАТИЧЕСКИ.\nАвтоматическое выключение через {AZAN_DURATION_MINUTES} минут.")
        time.sleep(AZAN_DURATION_MINUTES * 60)
        # Берём общий актуальный токен. Если Tuya вернёт 1010,
        # tuya_request сам получит новый токен и повторит команду один раз.
        token = get_socket_monitor_token()
        set_socket(token, False)
        telegram_notify(f"🔌 {PRAYER_NAMES_RU.get(prayer_name, prayer_name)}: розетка выключена автоматически.")

        # После команды выключения ещё минуту подтверждаем состояние.
        # В этот период проверка будет идти каждые 20 секунд.
        post_azan_monitor_until = time.time() + POST_AZAN_VERIFY_SECONDS
        # Сбрасываем отметку последней проверки, чтобы следующий цикл
        # не ждал остаток часового интервала.
        last_socket_check = None
    except Exception as error:
        # Если включение/выключение завершилось ошибкой, не считаем,
        # что розетка точно в нужном состоянии. Ещё минуту проверяем её
        # фактическое состояние каждые 20 секунд.
        post_azan_monitor_until = time.time() + POST_AZAN_VERIFY_SECONDS
        active_azan_monitor_until = 0
        last_socket_check = None
        print(f"❌ Ошибка во время {prayer_name}: {error}")
        telegram_notify(f"❌ Ошибка Azan Bot\n{prayer_name}: {error}\n\n🔎 Проверю состояние розетки ещё 1 минуту — раз в 20 секунд.")


def start_azan_async(token, prayer_name, prayer_time):
    threading.Thread(target=run_azan, args=(token, prayer_name, prayer_time), daemon=True).start()


# =========================
# ОСНОВНОЙ ЦИКЛ
# =========================

def main():
    global prepared_token, prepared_prayer_key

    check_environment()
    setup_telegram_commands()

    print("🚀 Azan Bot запущен")
    print("🌍 Часовой пояс: Europe/Moscow")
    print(f"⏱ Проверка времени: {CHECK_INTERVAL_SECONDS} сек.")
    print(f"🔑 Подготовка Tuya: за {PREPARE_MINUTES} мин.")
    print(f"🔌 Проверка в обычное время: каждые {SOCKET_STATUS_CHECK_SECONDS} сек.")
    print(f"🔊 Проверка во время азана: каждые {AZAN_STATUS_CHECK_SECONDS} сек.")
    print(f"🔎 Проверка после выключения: каждые {POST_AZAN_STATUS_CHECK_SECONDS} сек. в течение {POST_AZAN_VERIFY_SECONDS} сек.")

    handled_prayers = set()
    handled_date = None

    telegram_notify(
        "🟢 ШЕНДЖИЙСКАЯ МЕЧЕТЬ\n\n"
        "🤖 Система автоматического азана запущена\n"
        "🔌 Мониторинг розетки активен\n"
        "📡 Telegram управление активно\n"
        "⚙️ Режим: 24/7\n\n"
        "👇 Используйте кнопки управления",
        show_keyboard=True,
    )

    try:
        send_schedule_to_telegram()
    except Exception as error:
        print(f"⚠️ Не удалось отправить расписание при запуске: {error}")

    while True:
        try:
            process_telegram_commands()
            check_socket_manual_changes()

            now = datetime.now(TIMEZONE)

            # Новый календарный день: начинаем отсчёт событий заново.
            if handled_date != now.date():
                handled_prayers.clear()
                handled_date = now.date()

            # Следующий будущий азан используется только для подготовки Tuya.
            next_prayer = get_next_prayer()

            if next_prayer is not None:
                start_dt, prayer_name, prayer_time = next_prayer
                prayer_key = (now.date().isoformat(), prayer_name)
                minutes_until = (start_dt - now).total_seconds() / 60


            # Отдельно ищем уже наступившие события.
            # Это важно: get_next_prayer() показывает только будущее
            # и после 15:24 сам Аср из него исчезает.
            events = get_today_events()

            for prayer_name, event_time in events.items():
                prayer_key = (now.date().isoformat(), prayer_name)

                if prayer_key in handled_prayers:
                    continue

                difference = (now - event_time).total_seconds()

                if 0 <= difference <= 120:
                    print(
                        f"🕌 Время азана: {prayer_name} "
                        f"({event_time.strftime('%H:%M')})"
                    )

                    token = get_socket_monitor_token()

                    start_azan_async(
                        token,
                        prayer_name,
                        get_today_schedule().get(prayer_name),
                    )
                    handled_prayers.add(prayer_key)



                elif difference > 120:
                    handled_prayers.add(prayer_key)
                    print(
                        f"⏭️ {prayer_name} уже прошёл более чем на 2 минуты — пропускаем"
                    )

            time.sleep(CHECK_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            print("🛑 Сервер остановлен вручную")
            break

        except Exception as error:
            print(f"❌ Ошибка основного цикла: {error}")
            telegram_notify(
                f"❌ Azan Bot\nОшибка основного цикла:\n{error}"
            )
            time.sleep(30)


if __name__ == "__main__":
    main()
