import os
import json
import time
import hmac
import hashlib
import requests
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

# Проверяем локальное время каждые 5 секунд.
# Это НЕ запрос к Tuya.
CHECK_INTERVAL_SECONDS = 5

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

    raise RuntimeError(
        f"В {SCHEDULE_FILE} нет расписания на {date_string}"
    )


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


def tuya_request(token, method, path, body=""):
    timestamp = str(int(time.time() * 1000))

    body_hash = hashlib.sha256(
        body.encode()
    ).hexdigest()

    string_to_sign = (
        method
        + "\n"
        + body_hash
        + "\n\n"
        + path
    )

    sign_message = (
        TUYA_ACCESS_ID
        + token
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
        "access_token": token,
        "sign": sign,
        "t": timestamp,
        "sign_method": "HMAC-SHA256",
        "Content-Type": "application/json",
    }

    url = TUYA_BASE_URL + path

    if method == "GET":
        response = requests.get(
            url,
            headers=headers,
            timeout=15,
        )
    else:
        response = requests.post(
            url,
            headers=headers,
            data=body,
            timeout=15,
        )

    response.raise_for_status()

    data = response.json()

    if not data.get("success"):
        raise RuntimeError(
            f"Tuya API error: {data}"
        )

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
        if item["code"] in (
            "switch",
            "switch_1",
            "switch_led",
        ):
            return bool(item["value"])

    raise RuntimeError(
        "Не удалось найти состояние розетки"
    )


def set_socket(token, state):
    path = (
        f"/v1.0/devices/"
        f"{TUYA_DEVICE_ID}/commands"
    )

    body = json.dumps(
        {
            "commands": [
                {
                    "code": "switch",
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
# АЗАН
# =========================

def run_azan(
    token,
    prayer_name,
    prayer_time,
):
    print(
        f"🕌 Наступил {prayer_name}: "
        f"{prayer_time}"
    )

    try:
        current_state = get_socket_state(token)

        print(
            "🔌 Текущее состояние:",
            "ВКЛ" if current_state else "ВЫКЛ",
        )

        if current_state:
            # Человек уже включил розетку.
            # Повторно ВКЛ не отправляем.
            print(
                "ℹ️ Розетка уже включена. "
                "Повторное ВКЛ не отправляем."
            )
        else:
            set_socket(token, True)

        telegram_send(
            f"🕌 {prayer_name}\n"
            f"Азан: {prayer_time}\n"
            f"Розетка включена/уже была включена.\n"
            f"Автоматическое выключение через "
            f"{AZAN_DURATION_MINUTES} минут."
        )

        # Важно:
        # здесь мы НЕ опрашиваем Tuya каждые 5 секунд.
        # Просто ждём локально 6 минут.
        time.sleep(
            AZAN_DURATION_MINUTES * 60
        )

        # В любом случае выключаем.
        set_socket(token, False)

        telegram_send(
            f"🔌 {prayer_name}: "
            f"розетка выключена."
        )

        print(
            f"✅ {prayer_name}: цикл завершён"
        )

    except Exception as error:
        print(
            f"❌ Ошибка во время {prayer_name}:",
            error,
        )

        telegram_send(
            f"❌ Ошибка Azan Bot\n"
            f"{prayer_name}: {error}"
        )


# =========================
# ОСНОВНОЙ ЦИКЛ
# =========================

def main():
    check_environment()

    print("🚀 Azan Bot запущен")
    print("🌍 Часовой пояс: Europe/Moscow")
    print(
        f"⏱ Проверка времени: "
        f"{CHECK_INTERVAL_SECONDS} сек."
    )
    print(
        f"🔑 Подготовка Tuya: "
        f"за {PREPARE_MINUTES} мин."
    )

    handled_prayers = set()

    prepared_prayer = None
    prepared_token = None

    while True:
        try:
            now = datetime.now(TIMEZONE)

            next_prayer = get_next_prayer()

            if next_prayer is None:
                # Новый день.
                if now.hour == 0 and now.minute == 0:
                    handled_prayers.clear()

                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            start_dt, prayer_name, prayer_time = next_prayer

            prayer_key = (
                now.date().isoformat(),
                prayer_name,
            )

            minutes_until = (
                start_dt - now
            ).total_seconds() / 60

            # ==================================
            # ПОДГОТОВКА TUYA ЗА 5 МИНУТ
            # ==================================

            if (
                prepared_prayer != prayer_key
                and 0 < minutes_until <= PREPARE_MINUTES
            ):
                print(
                    f"🔑 До {prayer_name} "
                    f"осталось {minutes_until:.1f} мин."
                )

                prepared_token = get_tuya_token()
                prepared_prayer = prayer_key

                print(
                    f"✅ Tuya подготовлена "
                    f"для {prayer_name}"
                )

            # ==================================
            # ВРЕМЯ АЗАНА
            # ==================================

            if (
                now >= start_dt
                and prayer_key not in handled_prayers
            ):
                # Если token по какой-либо причине
                # не был подготовлен заранее —
                # получаем его сейчас.
                if prepared_token is None:
                    print(
                        "⚠️ Token не был подготовлен "
                        "заранее. Получаем сейчас."
                    )

                    prepared_token = get_tuya_token()

                run_azan(
                    prepared_token,
                    prayer_name,
                    prayer_time,
                )

                handled_prayers.add(
                    prayer_key
                )

                prepared_prayer = None
                prepared_token = None

            time.sleep(
                CHECK_INTERVAL_SECONDS
            )

        except Exception as error:
            print(
                "❌ Ошибка основного цикла:",
                error,
            )

            telegram_send(
                f"❌ Azan Bot\n"
                f"Ошибка основного цикла:\n"
                f"{error}"
            )

            time.sleep(30)


if __name__ == "__main__":
    main()
