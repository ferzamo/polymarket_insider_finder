import argparse
import io
import sqlite3
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch
from urllib.error import HTTPError

from polymarket_insider_finder import DEFAULT_RULES
from polymarket_insider_finder import EventSnapshotRecord
from polymarket_insider_finder import EventState
from polymarket_insider_finder import MarketSnapshot
from polymarket_insider_finder import MarketSnapshotRecord
from polymarket_insider_finder import Signal
from polymarket_insider_finder import detect_signals
from polymarket_insider_finder import ensure_db
from polymarket_insider_finder import fetch_json
from polymarket_insider_finder import hydrate_telegram_credentials
from polymarket_insider_finder import load_simple_env_file
from polymarket_insider_finder import parse_yes_no_prices
from polymarket_insider_finder import record_sent_alert
from polymarket_insider_finder import resolve_thresholds
from polymarket_insider_finder import should_send_alert


class ParsePricesTests(unittest.TestCase):
    def test_parse_yes_no_prices(self) -> None:
        market = {
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.61", "0.39"]',
        }
        self.assertEqual(parse_yes_no_prices(market), (0.61, 0.39))

    def test_non_binary_market_is_ignored(self) -> None:
        market = {
            "outcomes": '["A", "B"]',
            "outcomePrices": '["0.61", "0.39"]',
        }
        self.assertIsNone(parse_yes_no_prices(market))

    def test_load_simple_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / "telegram.env"
            env_file.write_text(
                "POLYMARKET_TELEGRAM_BOT_TOKEN=test-token\n"
                "POLYMARKET_TELEGRAM_CHAT_ID=12345\n",
                encoding="utf-8",
            )

            values = load_simple_env_file(env_file)

        self.assertEqual(values["POLYMARKET_TELEGRAM_BOT_TOKEN"], "test-token")
        self.assertEqual(values["POLYMARKET_TELEGRAM_CHAT_ID"], "12345")

    def test_hydrate_telegram_credentials(self) -> None:
        args = argparse.Namespace(telegram_bot_token="", telegram_chat_id="")

        hydrate_telegram_credentials(
            args,
            {
                "POLYMARKET_TELEGRAM_BOT_TOKEN": "token-from-file",
                "POLYMARKET_TELEGRAM_CHAT_ID": "chat-from-file",
            },
        )

        self.assertEqual(args.telegram_bot_token, "token-from-file")
        self.assertEqual(args.telegram_chat_id, "chat-from-file")


class HttpRetryTests(unittest.TestCase):
    def test_fetch_json_retries_after_http_429(self) -> None:
        headers = Message()
        headers["Retry-After"] = "7"
        rate_limited_error = HTTPError(
            url="https://example.com",
            code=429,
            msg="Too Many Requests",
            hdrs=headers,
            fp=None,
        )
        successful_response = MagicMock()
        successful_response.__enter__.return_value = io.StringIO('{"ok": true}')
        successful_response.__exit__.return_value = False

        with patch("polymarket_insider_finder.urlopen", side_effect=[rate_limited_error, successful_response]) as mock_urlopen:
            with patch("polymarket_insider_finder.time.sleep") as mock_sleep:
                payload = fetch_json("https://example.com")

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once_with(7.0)

    def test_fetch_json_does_not_retry_non_retryable_http_errors(self) -> None:
        not_found_error = HTTPError(
            url="https://example.com",
            code=404,
            msg="Not Found",
            hdrs=Message(),
            fp=None,
        )

        with patch("polymarket_insider_finder.urlopen", side_effect=not_found_error) as mock_urlopen:
            with patch("polymarket_insider_finder.time.sleep") as mock_sleep:
                with self.assertRaises(HTTPError):
                    fetch_json("https://example.com")

        self.assertEqual(mock_urlopen.call_count, 1)
        mock_sleep.assert_not_called()


class SignalTests(unittest.TestCase):
    def test_detect_signals_picks_aggressive_market(self) -> None:
        event = EventState(
            event_id="event-1",
            title="Election",
            slug="election",
            open_interest=120000.0,
            fetched_at=200,
            markets=[
                MarketSnapshot(
                    market_id="m1",
                    event_id="event-1",
                    event_title="Election",
                    event_slug="election",
                    question="Will X win?",
                    slug="x-win",
                    condition_id="cond-1",
                    yes_price=0.72,
                    no_price=0.28,
                    liquidity=10000.0,
                    volume_24h=3000.0,
                    event_open_interest=120000.0,
                    fee_type="general_fees",
                    fetched_at=200,
                ),
                MarketSnapshot(
                    market_id="m2",
                    event_id="event-1",
                    event_title="Election",
                    event_slug="election",
                    question="Will Y resign?",
                    slug="y-resign",
                    condition_id="cond-2",
                    yes_price=0.55,
                    no_price=0.45,
                    liquidity=10000.0,
                    volume_24h=3000.0,
                    event_open_interest=120000.0,
                    fee_type="general_fees",
                    fetched_at=200,
                ),
            ],
        )
        previous_events = {
            "event-1": EventSnapshotRecord("event-1", 100, 100000.0),
        }
        previous_markets = {
            "m1": MarketSnapshotRecord("m1", 100, 0.61, 0.39),
            "m2": MarketSnapshotRecord("m2", 100, 0.52, 0.48),
        }

        signals = detect_signals({"event-1": event}, previous_events, previous_markets, DEFAULT_RULES)

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].market_id, "m1")
        self.assertEqual(signals[0].direction, "YES")
        self.assertAlmostEqual(signals[0].price_move, 0.11)

    def test_resolve_thresholds_uses_fee_type_and_liquidity_band(self) -> None:
        market = MarketSnapshot(
            market_id="m1",
            event_id="event-1",
            event_title="Final",
            event_slug="final",
            question="Will Team A win?",
            slug="team-a-win",
            condition_id="cond-1",
            yes_price=0.55,
            no_price=0.45,
            liquidity=150000.0,
            volume_24h=3000.0,
            event_open_interest=200000.0,
            fee_type="sports_fees_v2",
            fetched_at=200,
        )

        thresholds = resolve_thresholds(DEFAULT_RULES, market)

        self.assertEqual(thresholds.min_oi_abs, 15000.0)
        self.assertEqual(thresholds.min_oi_pct, 0.025)
        self.assertEqual(thresholds.min_price_move, 0.04)
        self.assertIn("sports_fees_v2", thresholds.profile_name)
        self.assertIn("liq:deep", thresholds.profile_name)

    def test_alert_cooldown_deduplicates_market_direction(self) -> None:
        connection = sqlite3.connect(":memory:")
        ensure_db(connection)

        signal_now = Signal(
            event_id="event-1",
            event_title="Election",
            event_slug="election",
            market_id="m1",
            question="Will X win?",
            slug="x-win",
            direction="YES",
            previous_side_price=0.61,
            current_side_price=0.72,
            price_move=0.11,
            previous_yes_price=0.61,
            current_yes_price=0.72,
            oi_delta_abs=20000.0,
            oi_delta_pct=0.2,
            current_open_interest=120000.0,
            market_liquidity=10000.0,
            market_volume_24h=3000.0,
            market_fee_type="general_fees",
            threshold_profile_name="defaults + general_fees + liq:mid",
            interval_seconds=100,
            strength=0.5,
            fetched_at=200,
        )
        signal_later = Signal(
            event_id="event-1",
            event_title="Election",
            event_slug="election",
            market_id="m1",
            question="Will X win?",
            slug="x-win",
            direction="YES",
            previous_side_price=0.72,
            current_side_price=0.82,
            price_move=0.10,
            previous_yes_price=0.72,
            current_yes_price=0.82,
            oi_delta_abs=30000.0,
            oi_delta_pct=0.25,
            current_open_interest=150000.0,
            market_liquidity=10000.0,
            market_volume_24h=3000.0,
            market_fee_type="general_fees",
            threshold_profile_name="defaults + general_fees + liq:mid",
            interval_seconds=1900,
            strength=0.7,
            fetched_at=2200,
        )

        self.assertTrue(should_send_alert(connection, signal_now, 1800))
        record_sent_alert(connection, signal_now)
        self.assertFalse(should_send_alert(connection, signal_now, 1800))
        self.assertTrue(should_send_alert(connection, signal_later, 1800))

        connection.close()


if __name__ == "__main__":
    unittest.main()