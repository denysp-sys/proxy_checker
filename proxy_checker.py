#!/usr/bin/env python3
"""
Перевірка одного проксі (SOCKS5 / HTTP): підключення, пінг до Google, швидкість.

На вході — лише креди до проксі у форматі host:port:login:password (або host:port).
Тип проксі визначається автоматично (пробує socks5, потім http).

Приклад запуску:
    py proxy_checker.py 1.2.3.4:1080:user:pass
    py proxy_checker.py            # креди буде запитано з клавіатури

Скрипт виводить лише один рядок результату (і повертає код виходу):
    0 — "OK: Proxy is working fine"                (швидкість >= 15 Мбіт/с)
    1 — "ERROR: Invalid proxy credentials"         (невірні дані / не вдалося підключитися)
    2 — "ERROR: Speed is below 5 Mbit/s"
    3 — "ERROR: Speed is below 15 Mbit/s"
"""

import sys
import time
from urllib.parse import quote

import requests

IP_CHECK_URL = "https://api.ipify.org?format=json"
DOWNLOAD_URL = "https://speed.cloudflare.com/__down?bytes={size}"
UPLOAD_URL = "https://speed.cloudflare.com/__up"
GOOGLE_PING_URL = "https://www.google.com/generate_204"
PROXY_TYPES = ("socks5", "http")

CONNECT_TIMEOUT = 10          # сек
SPEED_TIMEOUT = 60            # сек
DOWNLOAD_SIZE = 10_000_000    # 10 МБ
UPLOAD_SIZE = 5_000_000       # 5 МБ
PING_COUNT = 5

MIN_SPEED_CRITICAL = 5        # Мбіт/с
MIN_SPEED_OK = 15             # Мбіт/с

EXIT_OK = 0
EXIT_BAD_CREDENTIALS = 1
EXIT_SPEED_CRITICAL = 2
EXIT_SPEED_LOW = 3


def parse_credential(credential: str):
    """host:port:login:password -> (host, port, login, password). Пароль може містити ':'."""
    parts = credential.strip().split(":", 3)
    if len(parts) == 2:
        host, port = parts
        login = password = ""
    elif len(parts) == 4:
        host, port, login, password = parts
    else:
        raise ValueError(f"Невірний формат кредів: {credential!r} (очікується host:port:login:password)")
    if not host:
        raise ValueError("Не вказано host")
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"Невірний порт: {port!r}")
    return host, int(port), login, password


def build_proxy_url(proxy_type: str, host: str, port: int, login: str, password: str) -> str:
    # socks5h — DNS резолвиться на стороні проксі
    scheme = "socks5h" if proxy_type == "socks5" else "http"
    auth = f"{quote(login, safe='')}:{quote(password, safe='')}@" if login else ""
    return f"{scheme}://{auth}{host}:{port}"


def check_connection(proxy_url: str, timeout: float):
    """Повертає (exit_ip, latency_ms) або кидає виняток."""
    proxies = {"http": proxy_url, "https": proxy_url}
    start = time.perf_counter()
    resp = requests.get(IP_CHECK_URL, proxies=proxies, timeout=timeout)
    latency = (time.perf_counter() - start) * 1000
    resp.raise_for_status()
    return resp.json().get("ip", "?"), latency


def measure_google_ping(proxy_url: str, count: int, timeout: float):
    """
    "Пінг" до Google через проксі — серія легких HTTPS-запитів в одному keep-alive
    з'єднанні (ICMP через проксі не проходить). Перший запит — прогрів.
    Повертає (min_ms, avg_ms, max_ms, jitter_ms, loss_percent).
    """
    times = []
    with requests.Session() as session:
        session.proxies = {"http": proxy_url, "https": proxy_url}
        session.get(GOOGLE_PING_URL, timeout=timeout).raise_for_status()  # прогрів
        for _ in range(count):
            start = time.perf_counter()
            try:
                session.get(GOOGLE_PING_URL, timeout=timeout).raise_for_status()
                times.append((time.perf_counter() - start) * 1000)
            except requests.RequestException:
                pass
    loss = (count - len(times)) / count * 100
    if not times:
        raise RuntimeError("усі пінги до Google втрачено")
    jitter = (sum(abs(a - b) for a, b in zip(times, times[1:])) / (len(times) - 1)
              if len(times) > 1 else 0.0)
    return min(times), sum(times) / len(times), max(times), jitter, loss


def measure_download(proxy_url: str, size: int, timeout: float) -> float:
    """Швидкість завантаження в Мбіт/с."""
    proxies = {"http": proxy_url, "https": proxy_url}
    received = 0
    start = time.perf_counter()
    with requests.get(DOWNLOAD_URL.format(size=size), proxies=proxies,
                      timeout=timeout, stream=True) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            received += len(chunk)
    elapsed = time.perf_counter() - start
    return received * 8 / elapsed / 1_000_000 if elapsed > 0 else 0.0


def measure_upload(proxy_url: str, size: int, timeout: float) -> float:
    """Швидкість вивантаження в Мбіт/с."""
    proxies = {"http": proxy_url, "https": proxy_url}
    payload = b"0" * size
    start = time.perf_counter()
    resp = requests.post(UPLOAD_URL, data=payload, proxies=proxies, timeout=timeout)
    elapsed = time.perf_counter() - start
    resp.raise_for_status()
    return size * 8 / elapsed / 1_000_000 if elapsed > 0 else 0.0


def finish(code: int, message: str):
    print(message)
    sys.exit(code)


def main():
    if len(sys.argv) > 1:
        credential = sys.argv[1]
    else:
        credential = input("Proxy credentials (host:port:login:password): ")

    # 1. Перевірка формату кредів
    try:
        host, port, login, password = parse_credential(credential)
    except ValueError:
        finish(EXIT_BAD_CREDENTIALS, "ERROR: Invalid proxy credentials")

    # 2. Підключення (автовизначення типу: socks5, потім http)
    proxy_url = None
    for candidate in PROXY_TYPES:
        url = build_proxy_url(candidate, host, port, login, password)
        try:
            check_connection(url, CONNECT_TIMEOUT)
        except Exception:
            continue
        proxy_url = url
        break

    if proxy_url is None:
        finish(EXIT_BAD_CREDENTIALS, "ERROR: Invalid proxy credentials")

    # 3. Пінг до Google (виконується завжди, на результат не впливає)
    try:
        measure_google_ping(proxy_url, PING_COUNT, CONNECT_TIMEOUT)
    except Exception:
        pass

    # 4. Швидкість завантаження
    try:
        download = measure_download(proxy_url, DOWNLOAD_SIZE, SPEED_TIMEOUT)
    except Exception:
        download = 0.0

    # 5. Швидкість вивантаження (виконується завжди, на результат не впливає)
    try:
        measure_upload(proxy_url, UPLOAD_SIZE, SPEED_TIMEOUT)
    except Exception:
        pass

    # Підсумок — за швидкістю завантаження
    if download < MIN_SPEED_CRITICAL:
        finish(EXIT_SPEED_CRITICAL, f"ERROR: Speed is below {MIN_SPEED_CRITICAL} Mbit/s")
    if download < MIN_SPEED_OK:
        finish(EXIT_SPEED_LOW, f"ERROR: Speed is below {MIN_SPEED_OK} Mbit/s")
    finish(EXIT_OK, "OK: Proxy is working fine")


if __name__ == "__main__":
    main()
