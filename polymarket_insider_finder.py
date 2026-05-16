#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import plistlib
import sqlite3
import sys
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GAMMA_KEYSET_URL = "https://gamma-api.polymarket.com/markets/keyset"
DEFAULT_DB_PATH = Path("data/polymarket_insider.sqlite3")
DEFAULT_RULES_PATH = Path("config/signal_rules.json")
DEFAULT_TELEGRAM_ENV_PATH = Path("config/telegram.env")
DEFAULT_LOG_PATH = Path("logs/polymarket_insider_finder.log")
DEFAULT_LAUNCHD_PLIST_PATH = Path("launchd/com.fernandozamora.polymarket-insider-finder.plist")
DEFAULT_RULES = {
    "defaults": {
        "min_oi_abs": 5000.0,
        "min_oi_pct": 0.04,
        "min_price_move": 0.06,
    },
    "fee_type_profiles": {
        "general_fees": {
            "min_oi_abs": 6000.0,
            "min_oi_pct": 0.045,
            "min_price_move": 0.065,
        },
        "culture_fees": {
            "min_oi_abs": 4500.0,
            "min_oi_pct": 0.04,
            "min_price_move": 0.06,
        },
        "sports_fees_v2": {
            "min_oi_abs": 9000.0,
            "min_oi_pct": 0.055,
            "min_price_move": 0.09,
        },
    },
    "liquidity_bands": [
        {
            "name": "deep",
            "min_liquidity": 100000.0,
            "thresholds": {
                "min_oi_abs": 15000.0,
                "min_oi_pct": 0.025,
                "min_price_move": 0.04,
            },
        },
        {
            "name": "mid",
            "min_liquidity": 10000.0,
            "thresholds": {
                "min_oi_abs": 8000.0,
                "min_oi_pct": 0.04,
                "min_price_move": 0.055,
            },
        },
        {
            "name": "thin",
            "min_liquidity": 0.0,
            "thresholds": {
                "min_oi_abs": 5000.0,
                "min_oi_pct": 0.06,
                "min_price_move": 0.09,
            },
        },
    ],
}
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PolymarketInsiderFinder/2.0)",
    "Accept": "application/json",
}
HTTP_RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
HTTP_MAX_ATTEMPTS = 4
HTTP_RETRY_BACKOFF_SECONDS = 5.0
HTTP_RETRY_BACKOFF_CAP_SECONDS = 60.0
PAGE_REQUEST_DELAY_SECONDS = 0.25


@dataclass(frozen=True)
class MarketSnapshot:
    market_id: str
    event_id: str
    event_title: str
    event_slug: str
    question: str
    slug: str
    condition_id: str
    yes_price: float
    no_price: float
    liquidity: float
    volume_24h: float
    event_open_interest: float
    fee_type: str
    fetched_at: int


@dataclass
class EventState:
    event_id: str
    title: str
    slug: str
    open_interest: float
    fetched_at: int
    markets: list[MarketSnapshot]


@dataclass(frozen=True)
class EventSnapshotRecord:
    event_id: str
    fetched_at: int
    open_interest: float


@dataclass(frozen=True)
class MarketSnapshotRecord:
    market_id: str
    fetched_at: int
    yes_price: float
    no_price: float


@dataclass(frozen=True)
class Thresholds:
    min_oi_abs: float
    min_oi_pct: float
    min_price_move: float
    profile_name: str


@dataclass(frozen=True)
class Signal:
    event_id: str
    event_title: str
    event_slug: str
    market_id: str
    question: str
    slug: str
    direction: str
    previous_side_price: float
    current_side_price: float
    price_move: float
    previous_yes_price: float
    current_yes_price: float
    oi_delta_abs: float
    oi_delta_pct: float
    current_open_interest: float
    market_liquidity: float
    market_volume_24h: float
    market_fee_type: str
    threshold_profile_name: str
    interval_seconds: int
    strength: float
    fetched_at: int


@dataclass(frozen=True)
class CycleResult:
    fetched_at: int
    event_count: int
    binary_market_count: int
    signals: list[Signal]
    had_baseline: bool
    summary_text: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Detecta posibles anomalías en Polymarket cuando sube el open interest "
            "del evento y el precio del YES o del NO se mueve con agresividad."
        )
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Segundos entre sondeos en modo watch o service. Recomendado: 60.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Ejecuta el monitor en bucle. Sin este flag hace una sola pasada.",
    )
    parser.add_argument(
        "--service",
        action="store_true",
        help="Modo servicio: activa watch y habilita logging rotativo a archivo.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=0,
        help="Limita las iteraciones en modo watch. 0 significa infinito.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Cantidad máxima de señales mostradas por iteración.",
    )
    parser.add_argument(
        "--limit-per-page",
        type=int,
        default=100,
        help="Tamaño de página para la API keyset de Gamma. Máximo útil: 100.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Limita páginas por pasada para pruebas. 0 significa sin límite.",
    )
    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help="Ruta al SQLite local usado para snapshots y deduplicación de alertas.",
    )
    parser.add_argument(
        "--rules-file",
        default=str(DEFAULT_RULES_PATH),
        help="JSON con perfiles por feeType y bandas de liquidez.",
    )
    parser.add_argument(
        "--min-oi-abs",
        type=float,
        default=5000.0,
        help="Valor base por defecto para el incremento absoluto de open interest.",
    )
    parser.add_argument(
        "--min-oi-pct",
        type=float,
        default=0.04,
        help="Valor base por defecto para el incremento porcentual de open interest.",
    )
    parser.add_argument(
        "--min-price-move",
        type=float,
        default=0.06,
        help="Valor base por defecto para el movimiento del YES o del NO.",
    )
    parser.add_argument(
        "--min-liquidity",
        type=float,
        default=2000.0,
        help="Liquidez mínima del mercado binario para entrar al análisis.",
    )
    parser.add_argument(
        "--min-volume-24h",
        type=float,
        default=250.0,
        help="Volumen mínimo de 24h del mercado binario para entrar al análisis.",
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Habilita notificaciones de señales nuevas por Telegram.",
    )
    parser.add_argument(
        "--telegram-env-file",
        default=str(DEFAULT_TELEGRAM_ENV_PATH),
        help="Archivo tipo .env desde el que cargar POLYMARKET_TELEGRAM_BOT_TOKEN y POLYMARKET_TELEGRAM_CHAT_ID.",
    )
    parser.add_argument(
        "--telegram-bot-token",
        default=os.getenv("POLYMARKET_TELEGRAM_BOT_TOKEN", ""),
        help="Token del bot de Telegram. También se puede pasar por env.",
    )
    parser.add_argument(
        "--telegram-chat-id",
        default=os.getenv("POLYMARKET_TELEGRAM_CHAT_ID", ""),
        help="Chat ID de Telegram. También se puede pasar por env.",
    )
    parser.add_argument(
        "--telegram-test-message",
        default="",
        help="Envía un mensaje de prueba a Telegram y sale.",
    )
    parser.add_argument(
        "--notify-top",
        type=int,
        default=3,
        help="Número máximo de señales nuevas incluidas en cada notificación.",
    )
    parser.add_argument(
        "--notification-cooldown",
        type=int,
        default=1800,
        help="Segundos mínimos antes de reenviar una alerta para el mismo mercado y dirección.",
    )
    parser.add_argument(
        "--log-file",
        default=str(DEFAULT_LOG_PATH),
        help="Archivo de log para modo service/watch.",
    )
    parser.add_argument(
        "--log-max-mb",
        type=float,
        default=5.0,
        help="Tamaño máximo en MB antes de rotar el log.",
    )
    parser.add_argument(
        "--log-backups",
        type=int,
        default=5,
        help="Cantidad de backups rotados a conservar.",
    )
    parser.add_argument(
        "--write-launchd-plist",
        action="store_true",
        help="Genera un plist de launchd para macOS y sale.",
    )
    parser.add_argument(
        "--launchd-plist-path",
        default=str(DEFAULT_LAUNCHD_PLIST_PATH),
        help="Ruta destino del plist de launchd.",
    )
    parser.add_argument(
        "--launchd-label",
        default="com.fernandozamora.polymarket-insider-finder",
        help="Label usado por launchd.",
    )
    return parser


def resolve_path(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def load_simple_env_file(env_file_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not env_file_path.exists():
        return values

    with env_file_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            clean_key = key.strip()
            clean_value = value.strip().strip('"').strip("'")
            if clean_key:
                values[clean_key] = clean_value

    return values


def hydrate_telegram_credentials(args: argparse.Namespace, env_values: dict[str, str]) -> None:
    if not args.telegram_bot_token:
        args.telegram_bot_token = env_values.get("POLYMARKET_TELEGRAM_BOT_TOKEN", "")
    if not args.telegram_chat_id:
        args.telegram_chat_id = env_values.get("POLYMARKET_TELEGRAM_CHAT_ID", "")


def configure_logger(args: argparse.Namespace, log_file_path: Path) -> logging.Logger:
    logger = logging.getLogger("polymarket_insider_finder")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console_handler)

    if args.watch or args.service:
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file_path,
            maxBytes=int(args.log_max_mb * 1024 * 1024),
            backupCount=args.log_backups,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(file_handler)

    return logger


def parse_retry_after_seconds(header_value: str | None) -> float | None:
    if not header_value:
        return None

    try:
        return max(0.0, float(header_value))
    except ValueError:
        pass

    try:
        retry_at = parsedate_to_datetime(header_value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None

    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)

    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def calculate_retry_delay(attempt_index: int, retry_after_header: str | None) -> float:
    retry_after_seconds = parse_retry_after_seconds(retry_after_header)
    if retry_after_seconds is not None:
        return retry_after_seconds

    return min(HTTP_RETRY_BACKOFF_CAP_SECONDS, HTTP_RETRY_BACKOFF_SECONDS * (2**attempt_index))


def fetch_json(url: str, params: dict[str, Any] | None = None) -> Any:
    if params:
        query = urlencode({key: value for key, value in params.items() if value is not None})
        request_url = f"{url}?{query}"
    else:
        request_url = url

    logger = logging.getLogger("polymarket_insider_finder")
    for attempt_index in range(HTTP_MAX_ATTEMPTS):
        request = Request(request_url, headers=HTTP_HEADERS)
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code not in HTTP_RETRY_STATUS_CODES or attempt_index + 1 >= HTTP_MAX_ATTEMPTS:
                raise

            delay_seconds = calculate_retry_delay(attempt_index, exc.headers.get("Retry-After") if exc.headers else None)
            logger.warning(
                "Gamma devolvio HTTP %s. Reintentando en %.1fs (%s/%s).",
                exc.code,
                delay_seconds,
                attempt_index + 1,
                HTTP_MAX_ATTEMPTS - 1,
            )
            time.sleep(delay_seconds)


def post_form_json(url: str, data: dict[str, Any]) -> Any:
    encoded = urlencode({key: value for key, value in data.items() if value is not None}).encode("utf-8")
    request = Request(url, data=encoded, headers={**HTTP_HEADERS, "Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def iter_active_markets(limit: int, max_pages: int) -> list[dict[str, Any]]:
    cursor: str | None = None
    pages = 0
    markets: list[dict[str, Any]] = []

    while True:
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
        }
        if cursor:
            params["after_cursor"] = cursor
        payload = fetch_json(GAMMA_KEYSET_URL, params)
        page_markets = payload.get("markets", [])
        if not page_markets:
            break
        markets.extend(page_markets)
        pages += 1
        cursor = payload.get("next_cursor")
        if not cursor:
            break
        if max_pages and pages >= max_pages:
            break
        time.sleep(PAGE_REQUEST_DELAY_SECONDS)

    return markets


def safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_json_array(raw_value: Any) -> list[Any]:
    if raw_value is None:
        return []
    if isinstance(raw_value, list):
        return raw_value
    if isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def parse_yes_no_prices(market: dict[str, Any]) -> tuple[float, float] | None:
    outcomes = parse_json_array(market.get("outcomes"))
    prices = parse_json_array(market.get("outcomePrices"))
    if len(outcomes) != 2 or len(prices) != 2:
        fallback_yes = market.get("lastTradePrice")
        if fallback_yes is None:
            return None
        yes_price = safe_float(fallback_yes)
        return yes_price, max(0.0, min(1.0, 1.0 - yes_price))

    mapping: dict[str, float] = {}
    for label, price in zip(outcomes, prices):
        if not isinstance(label, str):
            continue
        mapping[label.strip().lower()] = safe_float(price)

    if set(mapping) != {"yes", "no"}:
        return None

    return mapping["yes"], mapping["no"]


def extract_market_snapshot(raw_market: dict[str, Any], fetched_at: int) -> MarketSnapshot | None:
    event_list = raw_market.get("events") or []
    if not event_list:
        return None
    event = event_list[0]
    yes_no_prices = parse_yes_no_prices(raw_market)
    if yes_no_prices is None:
        return None

    yes_price, no_price = yes_no_prices
    event_id = str(event.get("id") or "")
    market_id = str(raw_market.get("id") or "")
    if not event_id or not market_id:
        return None

    return MarketSnapshot(
        market_id=market_id,
        event_id=event_id,
        event_title=str(event.get("title") or raw_market.get("question") or ""),
        event_slug=str(event.get("slug") or ""),
        question=str(raw_market.get("question") or ""),
        slug=str(raw_market.get("slug") or ""),
        condition_id=str(raw_market.get("conditionId") or ""),
        yes_price=yes_price,
        no_price=no_price,
        liquidity=safe_float(raw_market.get("liquidityNum") or raw_market.get("liquidity")),
        volume_24h=safe_float(raw_market.get("volume24hr") or raw_market.get("volume24hrClob")),
        event_open_interest=safe_float(event.get("openInterest")),
        fee_type=str(raw_market.get("feeType") or "unknown"),
        fetched_at=fetched_at,
    )


def build_event_states(
    raw_markets: list[dict[str, Any]],
    fetched_at: int,
    min_liquidity: float,
    min_volume_24h: float,
) -> tuple[dict[str, EventState], int]:
    events: dict[str, EventState] = {}
    binary_market_count = 0

    for raw_market in raw_markets:
        snapshot = extract_market_snapshot(raw_market, fetched_at)
        if snapshot is None:
            continue
        if snapshot.liquidity < min_liquidity:
            continue
        if snapshot.volume_24h < min_volume_24h:
            continue

        binary_market_count += 1
        event_state = events.get(snapshot.event_id)
        if event_state is None:
            event_state = EventState(
                event_id=snapshot.event_id,
                title=snapshot.event_title,
                slug=snapshot.event_slug,
                open_interest=snapshot.event_open_interest,
                fetched_at=fetched_at,
                markets=[],
            )
            events[snapshot.event_id] = event_state
        event_state.markets.append(snapshot)

    return events, binary_market_count


def ensure_db(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS event_snapshots (
            event_id TEXT NOT NULL,
            fetched_at INTEGER NOT NULL,
            title TEXT NOT NULL,
            slug TEXT NOT NULL,
            open_interest REAL NOT NULL,
            PRIMARY KEY (event_id, fetched_at)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS market_snapshots (
            market_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            fetched_at INTEGER NOT NULL,
            question TEXT NOT NULL,
            slug TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            yes_price REAL NOT NULL,
            no_price REAL NOT NULL,
            liquidity REAL NOT NULL,
            volume_24h REAL NOT NULL,
            PRIMARY KEY (market_id, fetched_at)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sent_alerts (
            signal_key TEXT PRIMARY KEY,
            sent_at INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            market_id TEXT NOT NULL,
            direction TEXT NOT NULL,
            last_strength REAL NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_snapshots_lookup ON event_snapshots (event_id, fetched_at DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_snapshots_lookup ON market_snapshots (market_id, fetched_at DESC)"
    )
    connection.commit()


def load_latest_event_snapshots(connection: sqlite3.Connection) -> dict[str, EventSnapshotRecord]:
    rows = connection.execute(
        """
        SELECT current.event_id, current.fetched_at, current.open_interest
        FROM event_snapshots AS current
        JOIN (
            SELECT event_id, MAX(fetched_at) AS max_fetched_at
            FROM event_snapshots
            GROUP BY event_id
        ) AS latest
          ON latest.event_id = current.event_id
         AND latest.max_fetched_at = current.fetched_at
        """
    ).fetchall()
    return {
        row[0]: EventSnapshotRecord(event_id=row[0], fetched_at=row[1], open_interest=row[2])
        for row in rows
    }


def load_latest_market_snapshots(connection: sqlite3.Connection) -> dict[str, MarketSnapshotRecord]:
    rows = connection.execute(
        """
        SELECT current.market_id, current.fetched_at, current.yes_price, current.no_price
        FROM market_snapshots AS current
        JOIN (
            SELECT market_id, MAX(fetched_at) AS max_fetched_at
            FROM market_snapshots
            GROUP BY market_id
        ) AS latest
          ON latest.market_id = current.market_id
         AND latest.max_fetched_at = current.fetched_at
        """
    ).fetchall()
    return {
        row[0]: MarketSnapshotRecord(
            market_id=row[0],
            fetched_at=row[1],
            yes_price=row[2],
            no_price=row[3],
        )
        for row in rows
    }


def persist_snapshots(connection: sqlite3.Connection, events: dict[str, EventState]) -> None:
    event_rows: list[tuple[Any, ...]] = []
    market_rows: list[tuple[Any, ...]] = []

    for event in events.values():
        event_rows.append(
            (
                event.event_id,
                event.fetched_at,
                event.title,
                event.slug,
                event.open_interest,
            )
        )
        for market in event.markets:
            market_rows.append(
                (
                    market.market_id,
                    market.event_id,
                    market.fetched_at,
                    market.question,
                    market.slug,
                    market.condition_id,
                    market.yes_price,
                    market.no_price,
                    market.liquidity,
                    market.volume_24h,
                )
            )

    with connection:
        connection.executemany(
            """
            INSERT OR REPLACE INTO event_snapshots (
                event_id,
                fetched_at,
                title,
                slug,
                open_interest
            ) VALUES (?, ?, ?, ?, ?)
            """,
            event_rows,
        )
        connection.executemany(
            """
            INSERT OR REPLACE INTO market_snapshots (
                market_id,
                event_id,
                fetched_at,
                question,
                slug,
                condition_id,
                yes_price,
                no_price,
                liquidity,
                volume_24h
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            market_rows,
        )


def merge_rules(base_rules: dict[str, Any], custom_rules: dict[str, Any]) -> dict[str, Any]:
    rules = deepcopy(base_rules)
    if "defaults" in custom_rules and isinstance(custom_rules["defaults"], dict):
        rules["defaults"].update(custom_rules["defaults"])
    if "fee_type_profiles" in custom_rules and isinstance(custom_rules["fee_type_profiles"], dict):
        for fee_type, profile in custom_rules["fee_type_profiles"].items():
            current = rules["fee_type_profiles"].get(fee_type, {})
            if isinstance(profile, dict):
                current.update(profile)
            rules["fee_type_profiles"][fee_type] = current
    if "liquidity_bands" in custom_rules and isinstance(custom_rules["liquidity_bands"], list):
        rules["liquidity_bands"] = custom_rules["liquidity_bands"]
    return rules


def load_rules(rules_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    rules = deepcopy(DEFAULT_RULES)
    if rules_path.exists():
        with rules_path.open("r", encoding="utf-8") as handle:
            custom_rules = json.load(handle)
        if not isinstance(custom_rules, dict):
            raise ValueError("El archivo de reglas debe ser un objeto JSON.")
        rules = merge_rules(rules, custom_rules)

    rules["defaults"].update(
        {
            "min_oi_abs": args.min_oi_abs,
            "min_oi_pct": args.min_oi_pct,
            "min_price_move": args.min_price_move,
        }
    )
    return rules


def resolve_thresholds(rules: dict[str, Any], market: MarketSnapshot) -> Thresholds:
    profile = deepcopy(rules.get("defaults", {}))
    profile_names = ["defaults"]

    fee_type_profiles = rules.get("fee_type_profiles", {})
    fee_type_profile = fee_type_profiles.get(market.fee_type)
    if isinstance(fee_type_profile, dict):
        profile.update(fee_type_profile)
        profile_names.append(market.fee_type)

    liquidity_bands = sorted(
        rules.get("liquidity_bands", []),
        key=lambda band: safe_float(band.get("min_liquidity")),
        reverse=True,
    )
    for band in liquidity_bands:
        if market.liquidity >= safe_float(band.get("min_liquidity")):
            thresholds = band.get("thresholds", {})
            if isinstance(thresholds, dict):
                profile.update(thresholds)
            profile_names.append(f"liq:{band.get('name', 'custom')}")
            break

    return Thresholds(
        min_oi_abs=safe_float(profile.get("min_oi_abs"), 5000.0),
        min_oi_pct=safe_float(profile.get("min_oi_pct"), 0.04),
        min_price_move=safe_float(profile.get("min_price_move"), 0.06),
        profile_name=" + ".join(profile_names),
    )


def calculate_signal_strength(price_move: float, oi_delta_abs: float, oi_delta_pct: float) -> float:
    return price_move * max(oi_delta_pct, 0.0) * math.log1p(max(oi_delta_abs, 0.0))


def detect_signals(
    events: dict[str, EventState],
    previous_events: dict[str, EventSnapshotRecord],
    previous_markets: dict[str, MarketSnapshotRecord],
    rules: dict[str, Any],
) -> list[Signal]:
    signals: list[Signal] = []

    for event in events.values():
        previous_event = previous_events.get(event.event_id)
        if previous_event is None or previous_event.open_interest <= 0:
            continue

        oi_delta_abs = event.open_interest - previous_event.open_interest
        oi_delta_pct = oi_delta_abs / previous_event.open_interest
        best_signal: Signal | None = None

        for market in event.markets:
            previous_market = previous_markets.get(market.market_id)
            if previous_market is None:
                continue

            thresholds = resolve_thresholds(rules, market)
            if oi_delta_abs < thresholds.min_oi_abs:
                continue
            if oi_delta_pct < thresholds.min_oi_pct:
                continue

            interval_seconds = market.fetched_at - previous_market.fetched_at
            if interval_seconds <= 0:
                continue

            yes_delta = market.yes_price - previous_market.yes_price
            if abs(yes_delta) < thresholds.min_price_move:
                continue

            if yes_delta >= 0:
                direction = "YES"
                previous_side_price = previous_market.yes_price
                current_side_price = market.yes_price
                price_move = yes_delta
            else:
                direction = "NO"
                previous_side_price = previous_market.no_price
                current_side_price = market.no_price
                price_move = market.no_price - previous_market.no_price

            strength = calculate_signal_strength(price_move, oi_delta_abs, oi_delta_pct)
            candidate = Signal(
                event_id=event.event_id,
                event_title=event.title,
                event_slug=event.slug,
                market_id=market.market_id,
                question=market.question,
                slug=market.slug,
                direction=direction,
                previous_side_price=previous_side_price,
                current_side_price=current_side_price,
                price_move=price_move,
                previous_yes_price=previous_market.yes_price,
                current_yes_price=market.yes_price,
                oi_delta_abs=oi_delta_abs,
                oi_delta_pct=oi_delta_pct,
                current_open_interest=event.open_interest,
                market_liquidity=market.liquidity,
                market_volume_24h=market.volume_24h,
                market_fee_type=market.fee_type,
                threshold_profile_name=thresholds.profile_name,
                interval_seconds=interval_seconds,
                strength=strength,
                fetched_at=market.fetched_at,
            )

            if best_signal is None or candidate.strength > best_signal.strength:
                best_signal = candidate

        if best_signal is not None:
            signals.append(best_signal)

    signals.sort(key=lambda signal: signal.strength, reverse=True)
    return signals


def format_timestamp(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_money(value: float) -> str:
    sign = "-" if value < 0 else ""
    absolute = abs(value)
    if absolute >= 1_000_000:
        return f"{sign}${absolute / 1_000_000:.2f}M"
    if absolute >= 1_000:
        return f"{sign}${absolute / 1_000:.1f}K"
    return f"{sign}${absolute:.0f}"


def render_signals(
    signals: list[Signal],
    event_count: int,
    binary_market_count: int,
    fetched_at: int,
    top: int,
) -> str:
    lines = [
        f"[{format_timestamp(fetched_at)}] Analizados {binary_market_count} mercados binarios en {event_count} eventos.",
    ]

    if not signals:
        lines.append("No hubo señales que superaran los umbrales en esta pasada.")
        return "\n".join(lines)

    lines.append(f"Señales detectadas: {min(len(signals), top)} de {len(signals)}")
    for index, signal in enumerate(signals[:top], start=1):
        lines.append(
            (
                f"{index}. {signal.question} | OI {format_money(signal.current_open_interest)} "
                f"({format_money(signal.oi_delta_abs)}, {signal.oi_delta_pct:.1%}) | "
                f"{signal.direction} {signal.previous_side_price:.3f} -> {signal.current_side_price:.3f} "
                f"(+{signal.price_move:.3f}) | liquidez {format_money(signal.market_liquidity)} | "
                f"feeType {signal.market_fee_type} | intervalo {signal.interval_seconds}s"
            )
        )
        lines.append(f"   Evento: {signal.event_title}")
        lines.append(f"   Perfil: {signal.threshold_profile_name}")
    return "\n".join(lines)


def signal_alert_key(signal: Signal) -> str:
    return f"{signal.market_id}:{signal.direction}"


def should_send_alert(connection: sqlite3.Connection, signal: Signal, cooldown_seconds: int) -> bool:
    row = connection.execute(
        "SELECT sent_at FROM sent_alerts WHERE signal_key = ?",
        (signal_alert_key(signal),),
    ).fetchone()
    if row is None:
        return True
    last_sent_at = int(row[0])
    return signal.fetched_at - last_sent_at >= cooldown_seconds


def record_sent_alert(connection: sqlite3.Connection, signal: Signal) -> None:
    with connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO sent_alerts (
                signal_key,
                sent_at,
                event_id,
                market_id,
                direction,
                last_strength
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                signal_alert_key(signal),
                signal.fetched_at,
                signal.event_id,
                signal.market_id,
                signal.direction,
                signal.strength,
            ),
        )


def market_url(signal: Signal) -> str:
    if signal.event_slug:
        return f"https://polymarket.com/event/{signal.event_slug}"
    return "https://polymarket.com"


def build_telegram_digest(signals: list[Signal]) -> str:
    lines = [
        "Polymarket Insider Finder",
        format_timestamp(signals[0].fetched_at),
        f"Señales nuevas: {len(signals)}",
        "",
    ]
    for index, signal in enumerate(signals, start=1):
        lines.append(f"{index}. {signal.question}")
        lines.append(f"Evento: {signal.event_title}")
        lines.append(
            f"{signal.direction} {signal.previous_side_price:.3f} -> {signal.current_side_price:.3f} (+{signal.price_move:.3f})"
        )
        lines.append(
            f"OI {format_money(signal.current_open_interest)} ({format_money(signal.oi_delta_abs)}, {signal.oi_delta_pct:.1%})"
        )
        lines.append(
            f"Liquidez {format_money(signal.market_liquidity)} | feeType {signal.market_fee_type} | intervalo {signal.interval_seconds}s"
        )
        lines.append(f"Perfil: {signal.threshold_profile_name}")
        lines.append(market_url(signal))
        lines.append("")
    return "\n".join(lines).strip()


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> None:
    response = post_form_json(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        },
    )
    if not response.get("ok"):
        raise RuntimeError(f"Telegram devolvió una respuesta inválida: {response}")


def notify_signals(connection: sqlite3.Connection, signals: list[Signal], args: argparse.Namespace, logger: logging.Logger) -> int:
    if not args.telegram:
        return 0
    if not args.telegram_bot_token or not args.telegram_chat_id:
        logger.warning(
            "Telegram está habilitado pero faltan POLYMARKET_TELEGRAM_BOT_TOKEN o POLYMARKET_TELEGRAM_CHAT_ID."
        )
        return 0

    eligible_signals: list[Signal] = []
    for signal in signals:
        if should_send_alert(connection, signal, args.notification_cooldown):
            eligible_signals.append(signal)
        if len(eligible_signals) >= args.notify_top:
            break

    if not eligible_signals:
        return 0

    send_telegram_message(
        bot_token=args.telegram_bot_token,
        chat_id=args.telegram_chat_id,
        text=build_telegram_digest(eligible_signals),
    )
    for signal in eligible_signals:
        record_sent_alert(connection, signal)
    return len(eligible_signals)


def run_cycle(connection: sqlite3.Connection, args: argparse.Namespace, rules: dict[str, Any]) -> CycleResult:
    fetched_at = int(time.time())
    raw_markets = iter_active_markets(limit=args.limit_per_page, max_pages=args.max_pages)
    events, binary_market_count = build_event_states(
        raw_markets=raw_markets,
        fetched_at=fetched_at,
        min_liquidity=args.min_liquidity,
        min_volume_24h=args.min_volume_24h,
    )
    previous_events = load_latest_event_snapshots(connection)
    previous_markets = load_latest_market_snapshots(connection)
    had_baseline = bool(previous_events and previous_markets)
    signals = detect_signals(events, previous_events, previous_markets, rules)
    persist_snapshots(connection, events)
    summary_text = render_signals(
        signals=signals,
        event_count=len(events),
        binary_market_count=binary_market_count,
        fetched_at=fetched_at,
        top=args.top,
    )
    if not had_baseline:
        summary_text = (
            summary_text + "\nPrimera muestra guardada. Necesitas una segunda iteración para medir deltas."
        )
    return CycleResult(
        fetched_at=fetched_at,
        event_count=len(events),
        binary_market_count=binary_market_count,
        signals=signals,
        had_baseline=had_baseline,
        summary_text=summary_text,
    )


def write_launchd_plist(
    args: argparse.Namespace,
    python_executable: str,
    script_path: Path,
    db_path: Path,
    rules_path: Path,
    telegram_env_path: Path,
    log_file_path: Path,
    plist_path: Path,
) -> Path:
    log_file_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    program_arguments = [
        python_executable,
        str(script_path),
        "--service",
        "--telegram",
        "--telegram-env-file",
        str(telegram_env_path),
        "--interval",
        str(args.interval),
        "--top",
        str(args.top),
        "--limit-per-page",
        str(args.limit_per_page),
        "--db-path",
        str(db_path),
        "--rules-file",
        str(rules_path),
        "--log-file",
        str(log_file_path),
        "--notify-top",
        str(args.notify_top),
        "--notification-cooldown",
        str(args.notification_cooldown),
        "--min-liquidity",
        str(args.min_liquidity),
        "--min-volume-24h",
        str(args.min_volume_24h),
        "--min-oi-abs",
        str(args.min_oi_abs),
        "--min-oi-pct",
        str(args.min_oi_pct),
        "--min-price-move",
        str(args.min_price_move),
    ]

    plist_payload = {
        "Label": args.launchd_label,
        "ProgramArguments": program_arguments,
        "WorkingDirectory": str(script_path.parent),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1",
        },
        "StandardOutPath": str(log_file_path.parent / "launchd.stdout.log"),
        "StandardErrorPath": str(log_file_path.parent / "launchd.stderr.log"),
    }

    with plist_path.open("wb") as handle:
        plistlib.dump(plist_payload, handle, sort_keys=False)

    return plist_path


def send_telegram_test_message(args: argparse.Namespace) -> int:
    if not args.telegram_bot_token or not args.telegram_chat_id:
        raise ValueError("Faltan credenciales de Telegram para enviar el mensaje de prueba.")
    send_telegram_message(args.telegram_bot_token, args.telegram_chat_id, args.telegram_test_message)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.service:
        args.watch = True
    if args.interval < 5:
        parser.error("--interval debe ser de al menos 5 segundos para evitar demasiado ruido.")
    if args.limit_per_page < 1 or args.limit_per_page > 100:
        parser.error("--limit-per-page debe estar entre 1 y 100.")
    if args.notification_cooldown < 0:
        parser.error("--notification-cooldown no puede ser negativo.")
    if args.log_max_mb <= 0:
        parser.error("--log-max-mb debe ser mayor que 0.")
    if args.log_backups < 0:
        parser.error("--log-backups no puede ser negativo.")

    script_path = Path(__file__).resolve()
    db_path = resolve_path(args.db_path)
    rules_path = resolve_path(args.rules_file)
    telegram_env_path = resolve_path(args.telegram_env_file)
    log_file_path = resolve_path(args.log_file)
    plist_path = resolve_path(args.launchd_plist_path)

    env_values = load_simple_env_file(telegram_env_path)
    hydrate_telegram_credentials(args, env_values)

    logger = configure_logger(args, log_file_path)

    if args.write_launchd_plist:
        generated_path = write_launchd_plist(
            args=args,
            python_executable=sys.executable,
            script_path=script_path,
            db_path=db_path,
            rules_path=rules_path,
            telegram_env_path=telegram_env_path,
            log_file_path=log_file_path,
            plist_path=plist_path,
        )
        logger.info(f"Plist generado en {generated_path}")
        return 0

    if args.telegram_test_message:
        try:
            send_telegram_test_message(args)
        except Exception as exc:
            logger.error(f"No se pudo enviar el mensaje de prueba a Telegram: {exc}")
            return 1
        logger.info("Mensaje de prueba enviado a Telegram.")
        return 0

    db_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules = load_rules(rules_path, args)

    connection = sqlite3.connect(db_path)
    try:
        ensure_db(connection)
        if not args.watch:
            result = run_cycle(connection, args, rules)
            logger.info(result.summary_text)
            if result.had_baseline:
                sent_count = notify_signals(connection, result.signals, args, logger)
                if sent_count:
                    logger.info(f"Alertas enviadas a Telegram: {sent_count}")
            return 0

        iteration = 0
        while True:
            iteration += 1
            try:
                result = run_cycle(connection, args, rules)
                logger.info(result.summary_text)
                if result.had_baseline:
                    sent_count = notify_signals(connection, result.signals, args, logger)
                    if sent_count:
                        logger.info(f"Alertas enviadas a Telegram: {sent_count}")
            except KeyboardInterrupt:
                logger.error("Interrumpido por el usuario.")
                return 130
            except Exception as exc:
                logger.error(f"Error en iteración {iteration}: {exc}")

            if args.iterations and iteration >= args.iterations:
                break
            time.sleep(args.interval)
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())