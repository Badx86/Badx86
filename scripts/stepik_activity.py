#!/usr/bin/env python3
"""
Генератор SVG-тепловой карты активности Stepik для GitHub profile README.

Тянет публичный эндпоинт https://stepik.org/api/user-activities/{user_id}
и собирает SVG в стиле GitHub contribution graph.

Пример:
    python stepik_activity.py --user-id 457012701 --theme dark \\
        --output stepik-activity-dark.svg
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

STEPIK_API = "https://stepik.org/api/user-activities/{user_id}"
TOKEN_URL = "https://stepik.org/oauth2/token/"

USER_AGENT = (
    "Mozilla/5.0 (stepik-activity-readme/1.0; +https://github.com/Badx86)"
)

# ----------------------------------------------------------------------
# Темы. Палитры — классические GitHub contribution graph.
# ----------------------------------------------------------------------
PALETTES: dict[str, dict[str, object]] = {
    "dark": {
        "text": "#c9d1d9",
        "muted": "#7d8590",
        "cells": ["#161b22", "#0e4429", "#006d32", "#26a641", "#39d353"],
    },
    "light": {
        "text": "#24292f",
        "muted": "#57606a",
        "cells": ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"],
    },
}

# Геометрия клеток (px)
CELL = 11
GAP = 3
LEFT_PAD = 32
TOP_PAD = 50
BOTTOM_PAD = 34
RIGHT_PAD = 12

MONTHS_EN = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]
WEEKDAY_LABELS = {0: "Mon", 2: "Wed", 4: "Fri"}  # ISO: Mon=0 ... Sun=6


# ----------------------------------------------------------------------
# Модель данных
# ----------------------------------------------------------------------
@dataclass(slots=True)
class ActivityData:
    pins: list[int]        # pins[0] = сегодня, pins[1] = вчера и т.д.
    today: date
    total_solved: int
    current_streak: int
    max_streak: int


# ----------------------------------------------------------------------
# Загрузка данных
# ----------------------------------------------------------------------
def _http_get_json(url: str, token: str | None = None, timeout: int = 30) -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_oauth_token(client_id: str, client_secret: str, timeout: int = 30) -> str:
    """Получает access_token через client_credentials flow."""
    from base64 import b64encode
    from urllib.parse import urlencode

    body = urlencode({"grant_type": "client_credentials"}).encode("ascii")
    basic = b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = Request(
        TOKEN_URL,
        data=body,
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        token = json.loads(resp.read().decode("utf-8")).get("access_token")
    if not token:
        raise RuntimeError("Stepik OAuth: access_token не получен")
    return token


def fetch_activity(user_id: int, token: str | None = None) -> ActivityData:
    """Качаем pins; при 401/403 пытаемся поднять OAuth из env."""
    url = STEPIK_API.format(user_id=user_id)
    try:
        payload = _http_get_json(url, token=token)
    except HTTPError as e:
        if e.code in (401, 403) and not token:
            cid = os.environ.get("STEPIK_CLIENT_ID")
            csec = os.environ.get("STEPIK_CLIENT_SECRET")
            if cid and csec:
                token = _get_oauth_token(cid, csec)
                payload = _http_get_json(url, token=token)
            else:
                raise RuntimeError(
                    f"Stepik API вернул {e.code}. Задай STEPIK_CLIENT_ID / "
                    "STEPIK_CLIENT_SECRET в GitHub Secrets, если профиль "
                    "недоступен анонимно."
                ) from e
        else:
            raise

    try:
        activity = payload["user-activities"][0]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Неожиданный ответ Stepik API: {payload!r}") from e

    pins = [int(x) for x in activity.get("pins") or []]
    if not pins:
        raise RuntimeError("Stepik не вернул pins — активности нет либо профиль скрыт")

    today = datetime.now(timezone.utc).date()

    # Текущая серия: сколько подряд ненулевых элементов с начала (pins[0] = сегодня).
    # Если сегодня 0, считаем серию с вчера (чтобы утренние часы не обнуляли стрик).
    current = 0
    start_idx = 1 if pins[0] == 0 else 0
    for v in pins[start_idx:]:
        if v > 0:
            current += 1
        else:
            break

    # Максимальная серия за весь массив.
    max_s = 0
    run = 0
    for v in pins:
        if v > 0:
            run += 1
            if run > max_s:
                max_s = run
        else:
            run = 0

    return ActivityData(
        pins=pins,
        today=today,
        total_solved=sum(pins),
        current_streak=current,
        max_streak=max_s,
    )


# ----------------------------------------------------------------------
# Разметка сетки
# ----------------------------------------------------------------------
def build_grid(
    data: ActivityData, weeks: int = 53,
) -> tuple[list[list[int | None]], list[date]]:
    """
    Возвращает матрицу [7 строк дней × weeks колонок] и список дат-понедельников
    для каждой колонки. None в ячейке = будущий день или вне окна.
    Последняя колонка содержит `today`.
    """
    today = data.today
    total_days = weeks * 7
    start = today - timedelta(days=total_days - 1)
    # Колонки выравниваем по понедельнику.
    while start.weekday() != 0:
        start += timedelta(days=1)

    grid: list[list[int | None]] = [[None] * weeks for _ in range(7)]
    col_dates: list[date] = []

    for col in range(weeks):
        week_start = start + timedelta(days=col * 7)
        col_dates.append(week_start)
        for row in range(7):
            d = week_start + timedelta(days=row)
            if d > today:
                continue
            delta = (today - d).days
            if 0 <= delta < len(data.pins):
                grid[row][col] = data.pins[delta]

    return grid, col_dates


def compute_thresholds(pins: list[int]) -> list[int]:
    """Квартили на ненулевых значениях → 4 границы уровней 1..4."""
    positives = sorted(v for v in pins if v > 0)
    if not positives:
        return [1, 2, 3, 4]
    n = len(positives)
    q = [positives[max(0, n * k // 4 - 1)] for k in (1, 2, 3, 4)]
    # Чиним возможные коллизии.
    out: list[int] = []
    prev = 0
    for v in q:
        v = max(v, prev + 1)
        out.append(v)
        prev = v
    return out[:4]


def level(count: int, thresholds: list[int]) -> int:
    if count <= 0:
        return 0
    for i, t in enumerate(thresholds):
        if count <= t:
            return i + 1
    return 4


def month_labels(col_dates: list[date]) -> list[tuple[int, str]]:
    """Лейблы месяцев для верхнего ряда: по первой неделе месяца, не ближе 3 колонок."""
    out: list[tuple[int, str]] = []
    last_month: int | None = None
    for col, d in enumerate(col_dates):
        if d.month != last_month:
            if not out or col - out[-1][0] >= 3:
                out.append((col, MONTHS_EN[d.month - 1]))
            last_month = d.month
    return out


# ----------------------------------------------------------------------
# Рендер SVG
# ----------------------------------------------------------------------
def _fmt_ru_count(n: int, forms: tuple[str, str, str]) -> str:
    """Простое склонение (один / два / пять)."""
    n_abs = abs(n) % 100
    if 11 <= n_abs <= 14:
        return forms[2]
    n_abs %= 10
    if n_abs == 1:
        return forms[0]
    if 2 <= n_abs <= 4:
        return forms[1]
    return forms[2]


def render_svg(data: ActivityData, theme: str = "dark", weeks: int = 53) -> str:
    palette = PALETTES[theme]
    cells_colors: list[str] = palette["cells"]  # type: ignore[assignment]
    text_c = palette["text"]
    muted_c = palette["muted"]

    grid, col_dates = build_grid(data, weeks)
    thresholds = compute_thresholds(data.pins)

    grid_w = weeks * (CELL + GAP) - GAP
    grid_h = 7 * (CELL + GAP) - GAP
    width = LEFT_PAD + grid_w + RIGHT_PAD
    height = TOP_PAD + grid_h + BOTTOM_PAD

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img" aria-label="Stepik activity heatmap">',
        '<style>'
        '.t{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",'
        'Helvetica,Arial,sans-serif;}'
        f'.title{{fill:{text_c};font-size:13px;font-weight:600;}}'
        f'.sub{{fill:{muted_c};font-size:11px;}}'
        f'.m{{fill:{muted_c};font-size:9px;}}'
        '</style>',
    ]

    # --- Заголовок
    solved_word = _fmt_ru_count(
        data.total_solved, ("задача", "задачи", "задач")
    )
    day_word = _fmt_ru_count(data.current_streak, ("день", "дня", "дней"))
    max_word = _fmt_ru_count(data.max_streak, ("день", "дня", "дней"))

    parts.append(
        f'<text class="t title" x="{LEFT_PAD}" y="20">'
        f'Stepik · {data.total_solved} {solved_word} решено'
        '</text>'
    )
    parts.append(
        f'<text class="t sub" x="{LEFT_PAD}" y="36">'
        f'Серия: {data.current_streak} {day_word} '
        f'· Максимум: {data.max_streak} {max_word}'
        '</text>'
    )

    origin_x = LEFT_PAD
    origin_y = TOP_PAD

    # --- Месяцы
    for col, lab in month_labels(col_dates):
        x = origin_x + col * (CELL + GAP)
        parts.append(f'<text class="t m" x="{x}" y="{origin_y - 6}">{lab}</text>')

    # --- Дни недели
    for row, lab in WEEKDAY_LABELS.items():
        y = origin_y + row * (CELL + GAP) + CELL - 1
        parts.append(f'<text class="t m" x="2" y="{y}">{lab}</text>')

    # --- Клетки
    for row in range(7):
        for col in range(weeks):
            count = grid[row][col]
            if count is None:
                continue
            lv = level(count, thresholds)
            color = cells_colors[lv]
            x = origin_x + col * (CELL + GAP)
            y = origin_y + row * (CELL + GAP)
            d = col_dates[col] + timedelta(days=row)
            tip = escape(f"{count} submissions · {d.isoformat()}")
            parts.append(
                f'<rect x="{x}" y="{y}" width="{CELL}" height="{CELL}" '
                f'rx="2" ry="2" fill="{color}">'
                f'<title>{tip}</title></rect>'
            )

    # --- Легенда
    legend_y = TOP_PAD + grid_h + 20
    legend_right = LEFT_PAD + grid_w
    legend_w = 5 * (CELL + GAP) - GAP + 80  # 5 ячеек + подписи
    legend_x = legend_right - legend_w

    parts.append(
        f'<text class="t m" x="{legend_x}" y="{legend_y}" '
        'text-anchor="start">Меньше</text>'
    )
    lx = legend_x + 44
    for i, color in enumerate(cells_colors):
        x = lx + i * (CELL + GAP)
        parts.append(
            f'<rect x="{x}" y="{legend_y - 10}" width="{CELL}" '
            f'height="{CELL}" rx="2" ry="2" fill="{color}"/>'
        )
    parts.append(
        f'<text class="t m" x="{lx + 5 * (CELL + GAP) + 4}" y="{legend_y}">'
        'Больше</text>'
    )

    parts.append("</svg>")
    return "".join(parts)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--user-id", type=int, required=True, help="Stepik user id")
    p.add_argument("--output", type=Path, required=True, help="Путь к SVG")
    p.add_argument(
        "--theme", choices=("dark", "light"), default="dark",
        help="Цветовая тема (default: dark)",
    )
    p.add_argument(
        "--weeks", type=int, default=53,
        help="Количество недель (default: 53)",
    )
    args = p.parse_args(argv)

    try:
        data = fetch_activity(args.user_id)
    except (HTTPError, URLError, RuntimeError) as e:
        print(f"[stepik_activity] Ошибка: {e}", file=sys.stderr)
        return 1

    svg = render_svg(data, theme=args.theme, weeks=args.weeks)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(svg, encoding="utf-8")
    print(
        f"[stepik_activity] {args.output} · "
        f"{data.total_solved} solved · "
        f"streak {data.current_streak} (max {data.max_streak})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
