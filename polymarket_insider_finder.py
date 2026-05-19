#!/usr/bin/env python3

from __future__ import annotations

import argparse
from html import escape as html_escape
import json
import logging
import math
import os
import plistlib
import re
import sqlite3
import sys
import time
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GAMMA_KEYSET_URL = "https://gamma-api.polymarket.com/markets/keyset"
POLYMARKET_TRADES_URL = "https://data-api.polymarket.com/trades"
POLYMARKET_PROFILE_URL_TEMPLATE = "https://polymarket.com/profile/{address}"
DEFAULT_DB_PATH = Path("data/polymarket_insider.sqlite3")
DEFAULT_RULES_PATH = Path("config/signal_rules.json")
DEFAULT_TELEGRAM_ENV_PATH = Path("config/telegram.env")
DEFAULT_LOG_PATH = Path("logs/polymarket_insider_finder.log")
DEFAULT_LAUNCHD_PLIST_PATH = Path("launchd/com.fernandozamora.polymarket-insider-finder.plist")
DEFAULT_BASELINE_MIN_AGE_SECONDS = 300
TRADE_LOOKUP_LIMIT = 50
TRADE_LOOKBACK_GRACE_SECONDS = 60
EXCLUDED_FEE_TYPES = frozenset({"sports_fees_v2"})
EXCLUDED_MARKET_CATEGORIES = frozenset({"crypto"})
TERMINAL_PRICE_EPSILON = 0.0005
CRYPTO_MARKET_PATTERNS = (
    re.compile(r"\bcrypto\b", re.IGNORECASE),
    re.compile(r"\bbitcoin\b", re.IGNORECASE),
    re.compile(r"\bbtc\b", re.IGNORECASE),
    re.compile(r"\bethereum\b", re.IGNORECASE),
    re.compile(r"\beth\b", re.IGNORECASE),
    re.compile(r"\bsolana\b", re.IGNORECASE),
    re.compile(r"\bdoge(?:coin)?\b", re.IGNORECASE),
    re.compile(r"\bxrp\b", re.IGNORECASE),
    re.compile(r"\bripple\b", re.IGNORECASE),
    re.compile(r"\bcardano\b", re.IGNORECASE),
    re.compile(r"\bada\b", re.IGNORECASE),
    re.compile(r"\blitecoin\b", re.IGNORECASE),
    re.compile(r"\bltc\b", re.IGNORECASE),
    re.compile(r"\bbinance\b", re.IGNORECASE),
    re.compile(r"\bbnb\b", re.IGNORECASE),
    re.compile(r"\bavalanche\b", re.IGNORECASE),
    re.compile(r"\bavax\b", re.IGNORECASE),
    re.compile(r"\btron\b", re.IGNORECASE),
    re.compile(r"\btrx\b", re.IGNORECASE),
    re.compile(r"\bshiba\b", re.IGNORECASE),
    re.compile(r"\bshib\b", re.IGNORECASE),
    re.compile(r"\bsui\b", re.IGNORECASE),
)
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
    condition_id: str = ""
    candidate_insider_account: str = ""
    candidate_insider_username: str = ""


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
        "--baseline-min-age",
        type=int,
        default=DEFAULT_BASELINE_MIN_AGE_SECONDS,
        help=(
            "Edad mínima en segundos del baseline usado para comparar precio y open interest. "
            "Si no existe un snapshot tan antiguo, se usa el último guardado."
        ),
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


def get_primary_event(raw_market: dict[str, Any]) -> dict[str, Any] | None:
    event_list = raw_market.get("events") or []
    if not event_list:
        return None

    event = event_list[0]
    return event if isinstance(event, dict) else None


def normalize_market_text(value: Any) -> str:
    return str(value or "").strip().lower()


def normalize_binary_outcome_label(value: Any) -> str:
    normalized = normalize_market_text(value)
    if normalized == "yes":
        return "YES"
    if normalized == "no":
        return "NO"
    return ""


def is_condition_id(value: str) -> bool:
    return bool(re.fullmatch(r"0x[a-fA-F0-9]{64}", value.strip()))


def is_wallet_address(value: str) -> bool:
    return bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", value.strip()))


def extract_market_category(raw_market: dict[str, Any]) -> str:
    event = get_primary_event(raw_market)
    candidates = [raw_market.get("category")]
    if event is not None:
        candidates.append(event.get("category"))

    for candidate in candidates:
        normalized = normalize_market_text(candidate)
        if normalized:
            return normalized

    return ""


def build_market_search_text(raw_market: dict[str, Any]) -> str:
    event = get_primary_event(raw_market)
    text_parts = [
        raw_market.get("question"),
        raw_market.get("slug"),
        raw_market.get("category"),
    ]
    if event is not None:
        text_parts.extend(
            [
                event.get("title"),
                event.get("slug"),
                event.get("category"),
            ]
        )

    tags = raw_market.get("tags")
    parsed_tags = parse_json_array(tags) if isinstance(tags, str) else tags
    if isinstance(parsed_tags, list):
        for tag in parsed_tags:
            if isinstance(tag, str):
                text_parts.append(tag)
            elif isinstance(tag, dict):
                text_parts.extend([tag.get("label"), tag.get("slug"), tag.get("name")])

    return " ".join(filter(None, (normalize_market_text(part) for part in text_parts)))


def is_crypto_market(raw_market: dict[str, Any]) -> bool:
    if extract_market_category(raw_market) in EXCLUDED_MARKET_CATEGORIES:
        return True

    haystack = build_market_search_text(raw_market)
    return any(pattern.search(haystack) for pattern in CRYPTO_MARKET_PATTERNS)


def is_terminal_probability(price: float) -> bool:
    return price <= TERMINAL_PRICE_EPSILON or price >= 1.0 - TERMINAL_PRICE_EPSILON


def is_effectively_resolved_market(market: MarketSnapshot) -> bool:
    return is_terminal_probability(market.yes_price) or is_terminal_probability(market.no_price)


def extract_market_snapshot(raw_market: dict[str, Any], fetched_at: int) -> MarketSnapshot | None:
    event = get_primary_event(raw_market)
    if event is None:
        return None
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
        fee_type=str(raw_market.get("feeType") or "unknown").strip().lower(),
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
        if is_crypto_market(raw_market):
            continue
        snapshot = extract_market_snapshot(raw_market, fetched_at)
        if snapshot is None:
            continue
        if snapshot.fee_type in EXCLUDED_FEE_TYPES:
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


def load_event_snapshots_at_or_before(
    connection: sqlite3.Connection, cutoff_fetched_at: int
) -> dict[str, EventSnapshotRecord]:
    rows = connection.execute(
        """
        SELECT current.event_id, current.fetched_at, current.open_interest
        FROM event_snapshots AS current
        JOIN (
            SELECT event_id, MAX(fetched_at) AS max_fetched_at
            FROM event_snapshots
            WHERE fetched_at <= ?
            GROUP BY event_id
        ) AS selected
          ON selected.event_id = current.event_id
         AND selected.max_fetched_at = current.fetched_at
        """,
        (cutoff_fetched_at,),
    ).fetchall()
    return {
        row[0]: EventSnapshotRecord(event_id=row[0], fetched_at=row[1], open_interest=row[2])
        for row in rows
    }


def load_market_snapshots_at_or_before(
    connection: sqlite3.Connection, cutoff_fetched_at: int
) -> dict[str, MarketSnapshotRecord]:
    rows = connection.execute(
        """
        SELECT current.market_id, current.fetched_at, current.yes_price, current.no_price
        FROM market_snapshots AS current
        JOIN (
            SELECT market_id, MAX(fetched_at) AS max_fetched_at
            FROM market_snapshots
            WHERE fetched_at <= ?
            GROUP BY market_id
        ) AS selected
          ON selected.market_id = current.market_id
         AND selected.max_fetched_at = current.fetched_at
        """,
        (cutoff_fetched_at,),
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


def load_comparison_event_snapshots(
    connection: sqlite3.Connection,
    reference_fetched_at: int,
    baseline_min_age: int,
) -> dict[str, EventSnapshotRecord]:
    latest_snapshots = load_latest_event_snapshots(connection)
    if baseline_min_age <= 0:
        return latest_snapshots

    comparison_snapshots = load_event_snapshots_at_or_before(connection, reference_fetched_at - baseline_min_age)
    for event_id, latest_snapshot in latest_snapshots.items():
        comparison_snapshots.setdefault(event_id, latest_snapshot)
    return comparison_snapshots


def load_comparison_market_snapshots(
    connection: sqlite3.Connection,
    reference_fetched_at: int,
    baseline_min_age: int,
) -> dict[str, MarketSnapshotRecord]:
    latest_snapshots = load_latest_market_snapshots(connection)
    if baseline_min_age <= 0:
        return latest_snapshots

    comparison_snapshots = load_market_snapshots_at_or_before(connection, reference_fetched_at - baseline_min_age)
    for market_id, latest_snapshot in latest_snapshots.items():
        comparison_snapshots.setdefault(market_id, latest_snapshot)
    return comparison_snapshots


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


def fetch_recent_condition_trades(condition_id: str, limit: int = TRADE_LOOKUP_LIMIT) -> list[dict[str, Any]]:
    if not is_condition_id(condition_id):
        return []

    payload = fetch_json(POLYMARKET_TRADES_URL, {"conditionId": condition_id, "limit": limit})
    if not isinstance(payload, list):
        return []
    return [trade for trade in payload if isinstance(trade, dict)]


def select_candidate_insider_account(signal: Signal, trades: list[dict[str, Any]]) -> str:
    target_outcome = signal.direction.upper()
    window_start = max(0, signal.fetched_at - signal.interval_seconds - TRADE_LOOKBACK_GRACE_SECONDS)
    window_end = signal.fetched_at + TRADE_LOOKBACK_GRACE_SECONDS
    ranked_candidates: list[tuple[bool, bool, float, int, str]] = []

    for trade in trades:
        wallet = str(trade.get("proxyWallet") or trade.get("makerAddress") or "").strip()
        if not wallet:
            continue
        if normalize_binary_outcome_label(trade.get("outcome")) != target_outcome:
            continue

        timestamp = int(safe_float(trade.get("timestamp")))
        in_window = window_start <= timestamp <= window_end if timestamp else False
        is_buy = normalize_market_text(trade.get("side")) == "buy"
        notional = safe_float(trade.get("size")) * max(0.0, safe_float(trade.get("price")))
        ranked_candidates.append((in_window, is_buy, notional, timestamp, wallet))

    if not ranked_candidates:
        return ""

    ranked_candidates.sort(key=lambda candidate: (candidate[0], candidate[1], candidate[2], candidate[3]), reverse=True)
    return ranked_candidates[0][4]


def fetch_polymarket_profile_html(account: str) -> str:
    if not is_wallet_address(account):
        return ""

    request = Request(
        POLYMARKET_PROFILE_URL_TEMPLATE.format(address=account),
        headers=HTTP_HEADERS,
    )
    with urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def extract_polymarket_username_from_profile_html(profile_html: str, account: str) -> str:
    if not profile_html or not is_wallet_address(account):
        return ""

    profile_html_lower = profile_html.lower()
    account_lower = account.lower()
    anchor = profile_html_lower.find(f'"proxyAddress":"{account_lower}"')
    if anchor == -1:
        anchor = profile_html_lower.find(account_lower)
    if anchor == -1:
        return ""

    window = profile_html[anchor : anchor + 2000]
    match = re.search(r'"username":"((?:\\.|[^\"])*)"', window)
    if match is None:
        return ""

    try:
        return str(json.loads(f'"{match.group(1)}"')).strip()
    except json.JSONDecodeError:
        return match.group(1).strip()


def resolve_polymarket_username(account: str) -> str:
    if not is_wallet_address(account):
        return ""

    profile_html = fetch_polymarket_profile_html(account)
    return extract_polymarket_username_from_profile_html(profile_html, account)


def attach_candidate_insider_accounts(signals: list[Signal]) -> list[Signal]:
    if not signals:
        return []

    logger = logging.getLogger("polymarket_insider_finder")
    trade_cache: dict[str, list[dict[str, Any]]] = {}
    username_cache: dict[str, str] = {}
    enriched_signals: list[Signal] = []

    for signal in signals:
        trades = trade_cache.get(signal.condition_id)
        if trades is None:
            try:
                trades = fetch_recent_condition_trades(signal.condition_id)
            except Exception as exc:
                logger.warning(
                    "No se pudieron cargar trades recientes para market_id=%s condition_id=%s: %s",
                    signal.market_id,
                    signal.condition_id,
                    exc,
                )
                trades = []
            trade_cache[signal.condition_id] = trades

        candidate_account = select_candidate_insider_account(signal, trades)
        candidate_username = ""
        if candidate_account:
            candidate_username = username_cache.get(candidate_account, "")
            if candidate_account not in username_cache:
                try:
                    candidate_username = resolve_polymarket_username(candidate_account)
                except Exception as exc:
                    logger.warning(
                        "No se pudo resolver el usuario de Polymarket para account=%s: %s",
                        candidate_account,
                        exc,
                    )
                    candidate_username = ""
                username_cache[candidate_account] = candidate_username

        enriched_signals.append(
            replace(
                signal,
                candidate_insider_account=candidate_account,
                candidate_insider_username=candidate_username,
            )
        )

    return enriched_signals


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
            if is_effectively_resolved_market(market):
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
                condition_id=market.condition_id,
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
        lines.append(f"   Cuenta candidata: {format_candidate_identity(signal)}")
    return "\n".join(lines)


def format_candidate_identity(signal: Signal) -> str:
    if signal.candidate_insider_username:
        return f"@{signal.candidate_insider_username}"
    if signal.candidate_insider_username:
        return f"@{signal.candidate_insider_username}"
    if signal.candidate_insider_account:
        return signal.candidate_insider_account
    return "no disponible"


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
        "<b>Polymarket Insider Finder</b>",
        f"<b>Actualizado:</b> <code>{html_escape(format_timestamp(signals[0].fetched_at))}</code>",
        f"<b>Señales nuevas:</b> {len(signals)}",
        "",
    ]
    for index, signal in enumerate(signals, start=1):
        oi_delta_abs = format_money(signal.oi_delta_abs)
        if signal.oi_delta_abs > 0:
            oi_delta_abs = f"+{oi_delta_abs}"

        lines.append(f"<b>{index}. {html_escape(signal.question)}</b>")
        lines.append(f"<b>Evento:</b> {html_escape(signal.event_title)}")
        lines.append(
            f"<b>Movimiento detectado:</b> {html_escape(signal.direction)} de {signal.previous_side_price:.3f} a {signal.current_side_price:.3f} ({signal.price_move:+.3f})"
        )
        lines.append(
            f"<b>Interes abierto:</b> {format_money(signal.current_open_interest)} | <b>Cambio:</b> {oi_delta_abs} ({signal.oi_delta_pct:+.1%})"
        )
        lines.append(
            f"<b>Liquidez:</b> {format_money(signal.market_liquidity)} | <b>Vol 24h:</b> {format_money(signal.market_volume_24h)}"
        )
        lines.append(
            f"<b>Perfil:</b> {html_escape(signal.threshold_profile_name)} | <b>Intervalo analizado:</b> {signal.interval_seconds}s"
        )
        if signal.candidate_insider_username:
            lines.append(f"<b>Cuenta candidata:</b> @{html_escape(signal.candidate_insider_username)}")
        elif signal.candidate_insider_account:
            lines.append(
                f"<b>Cuenta candidata:</b> <code>{html_escape(signal.candidate_insider_account)}</code>"
            )
        else:
            lines.append("<b>Cuenta candidata:</b> no disponible")
        lines.append(f"<a href=\"{html_escape(market_url(signal), quote=True)}\">Abrir mercado</a>")
        lines.append("")
    return "\n".join(lines).strip()


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> None:
    response = post_form_json(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
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
    previous_events = load_comparison_event_snapshots(
        connection=connection,
        reference_fetched_at=fetched_at,
        baseline_min_age=args.baseline_min_age,
    )
    previous_markets = load_comparison_market_snapshots(
        connection=connection,
        reference_fetched_at=fetched_at,
        baseline_min_age=args.baseline_min_age,
    )
    had_baseline = bool(previous_events and previous_markets)
    signals = attach_candidate_insider_accounts(detect_signals(events, previous_events, previous_markets, rules))
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
        "--baseline-min-age",
        str(args.baseline_min_age),
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
    send_telegram_message(args.telegram_bot_token, args.telegram_chat_id, html_escape(args.telegram_test_message))
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
    if args.baseline_min_age < 0:
        parser.error("--baseline-min-age no puede ser negativo.")
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