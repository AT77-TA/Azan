import os
import time
import json
import hmac
import hashlib
import logging
import requests

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup


# ============================================================
# НАСТРОЙКИ
# ============================================================

TUYA_ACCESS_ID = os.getenv("TUYA_ACCESS_ID")
TUYA_ACCESS_SECRET = os.getenv("TUYA_ACCESS_SECRET")
TUYA_DEVICE_ID = os.getenv("TUYA_DEVICE_ID")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TUYA_BASE_URL = "https://openapi.tuyaeu.com"

PRAYER_URL = "https://kogdanamaz.ru/default.php?city=MYKP"

# Всё приложение работает по московскому времени
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


# ============================================================
# НАСТРОЙКИ АВТОМАТИКИ
# ============================================================

# Через сколько минут после времени намаза запускать азан
START_DELAY_MINUTES = 2

# Сколько минут работает азан
AZAN_DURATION_MINUTES = 6

# Как часто проверять время
CHECK_INTERVAL_SECONDS = 5

# После какого времени обновлять расписание нового дня
SCHEDULE_REFRESH_HOUR = 1


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
# ГЛОБАЛЬНОЕ СОСТОЯНИЕ
# ============================================================

current_schedule = {}

schedule_loaded_date = None

executed_events = set()


# ============================================================
# ВРЕМЯ МОСКВА
# ============================================================

def now_moscow():
    return datetime.now(MOSCOW_TZ)


# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s"
)


def log(level, message):

    now = now_moscow()

    timestamp = now.strftime(
        "%d.%m.%Y %H:%M:%S"
    )

    logging.info(
        f"{timestamp} | {level} | {message}"
    )


# ============================================================
# TELEGRAM УВЕДОМЛЕНИЯ
# ============================================================

def telegram_notify(message):

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning(
            "Telegram не настроен: нет TOKEN или CHAT_ID"
        )
        return False

    try:

        url = (
            f"https://api.telegram.org/bot"
            f"{TELEGRAM_BOT_TOKEN}/sendMessage"
        )

        response = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message
            },
            timeout=15
        )

        response.raise_for_status()

        data = response.json()

        if not data.get("ok"):
            raise Exception(data)

        return True

    except Exception as error:

        log(
            "ERROR",
            f"❌ Ошибка Telegram: {error}"
        )

        return False


# ============================================================
# ПРОВЕРКА SECRETS
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
        "✅ Все Secrets Tuya найдены"
    )


# ============================================================
# ПОЛУЧЕНИЕ РАСПИСАНИЯ С САЙТА
# ============================================================

def get_prayer_times():

    log(
        "INFO",
        "📡 Получаем расписание намазов с сайта..."
    )

    response = requests.get(
        PRAYER_URL,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0"
        }
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    prayer_times = {}

    for table in soup.find_all("table"):

        for row in table.find_all("tr"):

            cells = row.find_all(
                ["td", "th"]
            )

            if len(cells) < 2:
                continue

            name = cells[0].get_text(
                strip=True
            )

            value = cells[1].get_text(
                strip=True
            )

            for prayer in PRAYERS:

                if prayer in name:

                    if ":" not in value:
                        continue

                    try:

                        parts = value.split(":")

                        hour = int(parts[0])
                        minute = int(parts[1])

                        if (
                            0 <= hour <= 23
                            and
                            0 <= minute <= 59
                        ):

                            prayer_times[prayer] = (
                                f"{hour:02d}:"
                                f"{minute:02d}"
                            )

                    except ValueError:
                        continue

    missing = [
        prayer
        for prayer in PRAYERS
        if prayer not in prayer_times
    ]

    if missing:

        raise Exception(
            "Не удалось получить расписание: "
            + ", ".join(missing)
        )

    return prayer_times


# ============================================================
# ОБНОВЛЕНИЕ РАСПИСАНИЯ
# ============================================================

def refresh_schedule():

    global current_schedule
    global schedule_loaded_date

    try:

        new_schedule = get_prayer_times()

        current_schedule = new_schedule

        schedule_loaded_date = (
            now_moscow().date()
        )

        log(
            "INFO",
            "💾 Расписание успешно обновлено"
        )

        for prayer in PRAYERS:

            log(
                "INFO",
                f"🕌 {prayer}: "
                f"{current_schedule[prayer]}"
            )

        return True

    except Exception as error:

        log(
            "ERROR",
            f"❌ Ошибка получения расписания: "
            f"{error}"
        )

        return False


# ============================================================
# TUYA SHA256
# ============================================================

def sha256(data):

    return hashlib.sha256(
        data.encode("utf-8")
    ).hexdigest()


# ============================================================
# СОЗДАНИЕ ПОДПИСИ TUYA
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

    content_hash = sha256(body)

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
        TUYA_ACCESS_SECRET.encode(
            "utf-8"
        ),
        sign_string.encode(
            "utf-8"
        ),
        hashlib.sha256
    ).hexdigest().upper()

    return timestamp, sign


# ============================================================
# ПОЛУЧЕНИЕ TOKEN TUYA
# ============================================================

def get_tuya_token():

    method = "GET"

    path = "/v1.0/token?grant_type=1"

    timestamp, sign = create_signature(
        method,
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
            f"Ошибка получения Tuya token: "
            f"{data}"
        )

    return data[
        "result"
    ][
        "access_token"
    ]


# ============================================================
# УПРАВЛЕНИЕ РОЗЕТКОЙ
# ============================================================

def get_socket_state(token):
    method = "GET"

    path = (
        f"/v1.0/iot-03/devices/"
        f"{TUYA_DEVICE_ID}/status"
    )

    timestamp, sign = create_signature(
        method,
        path,
        token
    )

    headers = {
        "client_id": TUYA_ACCESS_ID,
        "access_token": token,
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
            f"Ошибка получения состояния Tuya: {data}"
        )

    for item in data.get("result", []):
        if item.get("code") == "switch_1":
            return bool(item.get("value"))

    raise Exception(
        "Не найдено состояние switch_1"
    )


def set_socket(state, token):
    action = (
        "ВКЛЮЧАЕМ"
        if state
        else "ВЫКЛЮЧАЕМ"
    )

    log(
        "INFO",
        f"🔌 {action} РОЗЕТКУ..."
    )

    method = "POST"

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
            f"Ошибка управления Tuya: {data}"
        )

    if state:
        log(
            "INFO",
            "✅ РОЗЕТКА ВКЛЮЧЕНА"
        )
    else:
        log(
            "INFO",
            "✅ РОЗЕТКА ВЫКЛЮЧЕНА"
        )


# ============================================================
# СОЗДАНИЕ СОБЫТИЙ НА СЕГОДНЯ
# ============================================================

def get_today_events():

    events = {}

    now = now_moscow()

    today = now.date()

    for prayer in PRAYERS:

        prayer_time = current_schedule.get(
            prayer
        )

        if not prayer_time:
            continue

        hour, minute = map(
            int,
            prayer_time.split(":")
        )

        prayer_datetime = datetime(
            year=today.year,
            month=today.month,
            day=today.day,
            hour=hour,
            minute=minute,
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
# ВЫВОД РАСПИСАНИЯ
# ============================================================

def print_schedule():

    now = now_moscow()

    log(
        "INFO",
        "=" * 60
    )

    log(
        "INFO",
        f"📅 РАСПИСАНИЕ НА "
        f"{now.strftime('%d.%m.%Y')}"
    )

    events = get_today_events()

    for prayer in PRAYERS:

        original_time = current_schedule.get(
            prayer
        )

        event_time = events.get(
            prayer
        )

        if original_time and event_time:

            log(
                "INFO",
                f"🕌 {prayer}: "
                f"{original_time} → "
                f"запуск "
                f"{event_time.strftime('%H:%M')}"
            )

    log(
        "INFO",
        f"➕ Задержка: "
        f"{START_DELAY_MINUTES} мин."
    )

    log(
        "INFO",
        f"🔊 Длительность: "
        f"{AZAN_DURATION_MINUTES} мин."
    )

    log(
        "INFO",
        "🌍 Часовой пояс: Europe/Moscow"
    )

    log(
        "INFO",
        "=" * 60
    )


# ============================================================
# ПОИСК СЛЕДУЮЩЕГО АЗАНА
# ============================================================

def print_next_azan():

    now = now_moscow()

    events = get_today_events()

    future_events = []

    for prayer, event_time in events.items():

        if event_time > now:

            future_events.append(
                (
                    event_time,
                    prayer
                )
            )

    if future_events:

        future_events.sort(
            key=lambda x: x[0]
        )

        event_time, prayer = (
            future_events[0]
        )

        log(
            "INFO",
            f"🎯 Следующий азан: "
            f"{prayer}"
        )

        log(
            "INFO",
            f"⏳ Запуск в: "
            f"{event_time.strftime('%H:%M:%S')}"
        )

    else:

        log(
            "INFO",
            "🌙 На сегодня азанов больше нет"
        )


# ============================================================
# ЗАПУСК АЗАНА
# ============================================================

def run_azan(prayer):
    event_id = (
        f"{now_moscow().date()}_{prayer}"
    )

    if event_id in executed_events:
        log(
            "WARNING",
            f"⚠️ {prayer} уже запускался"
        )
        return

    # Отмечаем событие сразу, чтобы не произошло
    # повторного запуска этого азана.
    executed_events.add(event_id)

    log(
        "INFO",
        "!" * 60
    )

    log(
        "INFO",
        f"🕌 ВРЕМЯ АЗАНА: {prayer}"
    )

    log(
        "INFO",
        "!" * 60
    )

    try:
        # Получаем token ОДИН раз на один азан.
        token = get_tuya_token()

        # Проверяем текущее состояние розетки.
        socket_state = get_socket_state(token)

        log(
            "INFO",
            "🔌 Текущее состояние розетки: "
            f"{'ВКЛ' if socket_state else 'ВЫКЛ'}"
        )

        if socket_state:
            # Если человек уже включил розетку вручную,
            # повторную команду ВКЛ не отправляем.
            log(
                "INFO",
                "ℹ️ Розетка уже включена. "
                "Команду ВКЛ не отправляем."
            )

            telegram_notify(
                f"🕌 ШЕНДЖИЙСКАЯ МЕЧЕТЬ\n\n"
                f"🔊 АЗАН НАЧАЛСЯ\n\n"
                f"🕌 Намаз: {prayer}\n"
                f"🔌 Розетка уже была включена\n"
                f"⏱ Выключим через "
                f"{AZAN_DURATION_MINUTES} мин."
            )
        else:
            # Если розетка выключена — включаем её.
            set_socket(True, token)

            log(
                "INFO",
                "🔊 АЗАН НАЧАЛСЯ"
            )

            telegram_notify(
                f"🕌 ШЕНДЖИЙСКАЯ МЕЧЕТЬ\n\n"
                f"🔊 АЗАН НАЧАЛСЯ\n\n"
                f"🕌 Намаз: {prayer}\n"
                f"🔌 Розетка включена\n"
                f"⏱ Длительность: "
                f"{AZAN_DURATION_MINUTES} мин."
            )

        log(
            "INFO",
            f"⏱️ Работаем {AZAN_DURATION_MINUTES} минут"
        )

        # Ждём 6 минут.
        time.sleep(
            AZAN_DURATION_MINUTES * 60
        )

        # Через 6 минут ВСЕГДА отправляем ВЫКЛ.
        # Неважно, включили розетку мы или человек.
        log(
            "INFO",
            "🔌 6 минут прошло. Выключаем розетку..."
        )

        set_socket(False, token)

        log(
            "INFO",
            f"🏁 АЗАН ЗАВЕРШЁН: {prayer}"
        )

        telegram_notify(
            f"🕌 ШЕНДЖИЙСКАЯ МЕЧЕТЬ\n\n"
            f"⏹ АЗАН ЗАВЕРШЁН\n\n"
            f"🕌 Намаз: {prayer}\n"
            f"🔌 Розетка выключена\n"
            f"🕐 Время: "
            f"{now_moscow().strftime('%H:%M:%S')}"
        )

    except Exception as error:
        log(
            "ERROR",
            f"❌ Ошибка во время азана: {error}"
        )

        telegram_notify(
            "⚠️ ОШИБКА ВО ВРЕМЯ АЗАНА\n\n"
            f"🕌 Намаз: {prayer}\n"
            f"❌ Ошибка: {error}\n"
            f"🕐 {now_moscow().strftime('%d.%m.%Y %H:%M:%S')}"
        )


# ============================================================
# ОБНОВЛЕНИЕ РАСПИСАНИЯ НОВОГО ДНЯ
# ============================================================

def check_new_day():

    global executed_events

    now = now_moscow()

    today = now.date()

    # Если расписание уже на сегодняшний день
    # ничего не делаем

    if schedule_loaded_date == today:

        return

    # До 01:00 не обновляем

    if now.hour < SCHEDULE_REFRESH_HOUR:

        return

    log(
        "INFO",
        "🌙 Новый день. Обновляем расписание..."
    )

    # Очищаем выполненные события прошлого дня

    executed_events.clear()

    success = refresh_schedule()

    if success:

        print_schedule()

        print_next_azan()

    else:

        log(
            "ERROR",
            "❌ Не удалось обновить расписание"
        )

        telegram_notify(
            "⚠️ ОШИБКА ОБНОВЛЕНИЯ РАСПИСАНИЯ\n\n"
            "Не удалось получить актуальное расписание намазов.\n"
            f"🕐 {now_moscow().strftime('%d.%m.%Y %H:%M:%S')}"
        )


# ============================================================
# ОСНОВНОЙ БОЕВОЙ РЕЖИМ
# ============================================================

def run_production_mode():

    log(
        "INFO",
        "=" * 60
    )

    log(
        "INFO",
        "🕌 АВТОМАТИЧЕСКИЙ АЗАН — МЕЧЕТЬ"
    )

    log(
        "INFO",
        "🤖 БОЕВОЙ РЕЖИМ 24/7"
    )

    log(
        "INFO",
        "=" * 60
    )

    # Проверяем Secrets

    check_secrets()

    # Первоначально получаем расписание

    log(
        "INFO",
        "📅 Получаем актуальное расписание..."
    )

    success = refresh_schedule()

    if not success:

        raise Exception(
            "Не удалось получить расписание "
            "при запуске"
        )

    print_schedule()

    print_next_azan()

    log(
        "INFO",
        "=" * 60
    )

    log(
        "INFO",
        "🟢 БОТ ЗАПУЩЕН И ГОТОВ К РАБОТЕ"
    )

    telegram_notify(
        "🟢 ШЕНДЖИЙСКАЯ МЕЧЕТЬ\n\n"
        "Система автоматического азана запущена.\n\n"
        f"📅 Дата: {now_moscow().strftime('%d.%m.%Y')}\n"
        "📡 Расписание загружено\n"
        "🔌 Tuya подключена\n"
        "🤖 Сервер работает 24/7"
    )

    log(
        "INFO",
        "🤖 Режим: 24/7"
    )

    log(
        "INFO",
        "🕌 Ожидаем время намаза"
    )

    log(
        "INFO",
        "=" * 60
    )

    # Бесконечная работа

    while True:

        try:

            # Проверяем смену дня

            check_new_day()

            # Получаем время

            now = now_moscow()

            # Получаем события

            events = get_today_events()

            # Проверяем каждый намаз

            for prayer, event_time in events.items():

                event_id = (
                    f"{now.date()}_{prayer}"
                )

                # Уже выполняли — пропускаем

                if event_id in executed_events:
                    continue

                # Если время наступило

                if now >= event_time:

                    difference = (
                        now - event_time
                    ).total_seconds()

                    # Допустимое окно 2 минуты

                    if 0 <= difference <= 120:

                        run_azan(prayer)

                    # Если сервер был выключен
                    # долгое время — не запускаем
                    # азан задним числом

                    elif difference > 120:

                        executed_events.add(event_id)

                        log(
                            "WARNING",
                            f"⚠️ {prayer} пропущен. "
                            f"Сервер был недоступен "
                            f"во время запуска."
                        )

            time.sleep(
                CHECK_INTERVAL_SECONDS
            )

        except KeyboardInterrupt:

            log(
                "INFO",
                "🛑 Бот остановлен вручную"
            )

            break

        except Exception as error:

            log(
                "ERROR",
                f"❌ Ошибка основного цикла: "
                f"{error}"
            )

            telegram_notify(
                "⚠️ ОШИБКА СИСТЕМЫ АЗАНА\n\n"
                f"❌ {error}\n\n"
                f"🕐 {now_moscow().strftime('%d.%m.%Y %H:%M:%S')}\n"
                "🔄 Повтор через 30 секунд..."
            )

            log(
                "INFO",
                "🔄 Повтор через 30 секунд..."
            )

            time.sleep(30)


# ============================================================
# ЗАПУСК
# ============================================================

def main():

    run_production_mode()


if __name__ == "__main__":
    main()
