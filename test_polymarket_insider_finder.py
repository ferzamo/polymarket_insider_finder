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
from polymarket_insider_finder import build_event_states
from polymarket_insider_finder import detect_signals
from polymarket_insider_finder import ensure_db
from polymarket_insider_finder import fetch_json
from polymarket_insider_finder import hydrate_telegram_credentials
from polymarket_insider_finder import load_simple_env_file
from polymarket_insider_finder import parse_yes_no_prices
from polymarket_insider_finder import persist_snapshots
from polymarket_insider_finder import record_sent_alert
from polymarket_insider_finder import resolve_thresholds
from polymarket_insider_finder import run_cycle
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
            fee_type="general_fees",
            fetched_at=200,
        )

        thresholds = resolve_thresholds(DEFAULT_RULES, market)

        self.assertEqual(thresholds.min_oi_abs, 15000.0)
        self.assertEqual(thresholds.min_oi_pct, 0.025)
        self.assertEqual(thresholds.min_price_move, 0.04)
        self.assertIn("general_fees", thresholds.profile_name)
        self.assertIn("liq:deep", thresholds.profile_name)

    def test_build_event_states_excludes_sports_fee_type(self) -> None:
        raw_markets = [
            {
                "id": "sports-market",
                "question": "Will Team A win?",
                "slug": "team-a-win",
                "conditionId": "cond-sports",
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["0.51", "0.49"]',
                "liquidityNum": 5000,
                "volume24hr": 1500,
                "feeType": "sports_fees_v2",
                "events": [
                    {
                        "id": "sports-event",
                        "title": "Sports Event",
                        "slug": "sports-event",
                        "openInterest": 25000,
                    }
                ],
            },
            {
                "id": "news-market",
                "question": "Will Candidate X win?",
                "slug": "candidate-x-win",
                "conditionId": "cond-news",
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["0.64", "0.36"]',
                "liquidityNum": 5000,
                "volume24hr": 1500,
                "feeType": "general_fees",
                "events": [
                    {
                        "id": "news-event",
                        "title": "Election",
                        "slug": "election",
                        "openInterest": 40000,
                    }
                ],
            },
        ]

        events, binary_market_count = build_event_states(
            raw_markets=raw_markets,
            fetched_at=200,
            min_liquidity=0.0,
            min_volume_24h=0.0,
        )

        self.assertEqual(binary_market_count, 1)
        self.assertEqual(set(events), {"news-event"})
        self.assertEqual(events["news-event"].markets[0].market_id, "news-market")

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

    def test_run_cycle_uses_older_baseline_when_available(self) -> None:
        connection = sqlite3.connect(":memory:")
        ensure_db(connection)

        older_event = EventState(
            event_id="event-1",
            title="Election",
            slug="election",
            open_interest=10000.0,
            fetched_at=100,
            markets=[
                MarketSnapshot(
                    market_id="m1",
                    event_id="event-1",
                    event_title="Election",
                    event_slug="election",
                    question="Will X win?",
                    slug="x-win",
                    condition_id="cond-1",
                    yes_price=0.40,
                    no_price=0.60,
                    liquidity=10000.0,
                    volume_24h=3000.0,
                    event_open_interest=10000.0,
                    fee_type="general_fees",
                    fetched_at=100,
                )
            ],
        )
        recent_event = EventState(
            event_id="event-1",
            title="Election",
            slug="election",
            open_interest=10800.0,
            fetched_at=260,
            markets=[
                MarketSnapshot(
                    market_id="m1",
                    event_id="event-1",
                    event_title="Election",
                    event_slug="election",
                    question="Will X win?",
                    slug="x-win",
                    condition_id="cond-1",
                    yes_price=0.45,
                    no_price=0.55,
                    liquidity=10000.0,
                    volume_24h=3000.0,
                    event_open_interest=10800.0,
                    fee_type="general_fees",
                    fetched_at=260,
                )
            ],
        )
        persist_snapshots(connection, {older_event.event_id: older_event})
        persist_snapshots(connection, {recent_event.event_id: recent_event})

        args = argparse.Namespace(
            limit_per_page=100,
            max_pages=1,
            min_liquidity=0.0,
            min_volume_24h=0.0,
            top=10,
            baseline_min_age=300,
        )

        current_markets = [
            {
                "id": "m1",
                "question": "Will X win?",
                "slug": "x-win",
                "conditionId": "cond-1",
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["0.50", "0.50"]',
                "liquidityNum": 10000,
                "volume24hr": 3000,
                "feeType": "general_fees",
                "events": [
                    {
                        "id": "event-1",
                        "title": "Election",
                        "slug": "election",
                        "openInterest": 12000,
                    }
                ],
            }
        ]

        custom_rules = {
            "defaults": {
                "min_oi_abs": 1000.0,
                "min_oi_pct": 0.1,
                "min_price_move": 0.08,
            },
            "fee_type_profiles": {},
            "liquidity_bands": [],
        }

        with patch("polymarket_insider_finder.time.time", return_value=400):
            with patch("polymarket_insider_finder.iter_active_markets", return_value=current_markets):
                result = run_cycle(connection, args, custom_rules)

        self.assertTrue(result.had_baseline)
        self.assertEqual(len(result.signals), 1)
        self.assertEqual(result.signals[0].market_id, "m1")
        self.assertEqual(result.signals[0].interval_seconds, 300)
        self.assertAlmostEqual(result.signals[0].price_move, 0.10)

        connection.close()


if __name__ == "__main__":
    unittest.main()