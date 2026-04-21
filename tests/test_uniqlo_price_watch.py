from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from scripts.uniqlo_price_watch import (
    AlertRecord,
    MailConfig,
    build_email_message,
    deduplicate_products,
    fetch_products,
    filter_target_products,
    find_new_products,
    load_state,
    main,
    normalize_product,
    parse_page_response,
    process_watch,
    save_state,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """读取 JSON 测试夹具。"""

    fixture_path = FIXTURES_DIR / name
    return cast(dict[str, Any], json.loads(fixture_path.read_text(encoding="utf-8")))


class UniqloPriceWatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page_1 = load_fixture("uniqlo_search_page_1.json")
        self.page_2 = load_fixture("uniqlo_search_page_2.json")
        self.config = MailConfig(
            mail_from="523738274@qq.com",
            mail_to="523738274@qq.com",
            smtp_auth_code="secret",
        )

    def test_filters_and_detects_new_products_across_pages(self) -> None:
        fetch_calls: list[int] = []

        def fetcher(page: int) -> tuple[list[dict[str, Any]], int]:
            fetch_calls.append(page)
            payload = self.page_1 if page == 1 else self.page_2
            return parse_page_response(payload)

        all_products = filter_target_products(fetch_products(fetcher))

        self.assertEqual(fetch_calls, [1, 2])
        self.assertEqual([product.code for product in all_products], ["480695", "481670"])
        existing_state = {
            "481670": AlertRecord(
                product_code="u0000000063579",
                name="UT品牌合作系列印花T恤/短袖T恤",
                alerted_price=59.0,
                alerted_at="2026-04-21T00:00:00+00:00",
            )
        }
        new_products = find_new_products(all_products, existing_state)
        self.assertEqual([product.code for product in new_products], ["480695"])

    def test_build_email_message_uses_inline_and_remote_images(self) -> None:
        payload, _ = parse_page_response(self.page_1)
        second_page, _ = parse_page_response(self.page_2)
        products = filter_target_products(
            deduplicate_products(
                normalize_product(item) for item in payload + second_page
            )
        )

        def image_fetcher(image_url: str) -> bytes | None:
            if "0061836" in image_url:
                return b"fake-image"
            return None

        message = build_email_message(products, self.config, image_fetcher)
        html_part = next(
            part for part in message.walk() if part.get_content_type() == "text/html"
        )
        html_content = html_part.get_content()

        self.assertIn("cid:", html_content)
        self.assertIn(
            "https://www.uniqlo.cn/hmall/test/u0000000063579/main/first/561/1.jpg",
            html_content,
        )
        image_parts = [part for part in message.walk() if part.get_content_maintype() == "image"]
        self.assertEqual(len(image_parts), 1)

    def test_process_watch_keeps_state_when_send_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            state_path = Path(temporary_dir) / "state.json"
            save_state(state_path, {})

            def fetcher(page: int) -> tuple[list[dict[str, Any]], int]:
                payload = self.page_1 if page == 1 else self.page_2
                return parse_page_response(payload)

            def image_fetcher(_: str) -> bytes | None:
                return None

            def sender(_: EmailMessage, __: MailConfig) -> None:
                raise RuntimeError("smtp error")

            with self.assertRaisesRegex(RuntimeError, "smtp error"):
                process_watch(
                    config=self.config,
                    state_path=state_path,
                    fetcher=fetcher,
                    image_fetcher=image_fetcher,
                    sender=sender,
                    now=datetime(2026, 4, 21, tzinfo=UTC),
                )

            self.assertEqual(load_state(state_path), {})

    def test_process_watch_updates_state_after_successful_send(self) -> None:
        sent_messages: list[str] = []

        with tempfile.TemporaryDirectory() as temporary_dir:
            state_path = Path(temporary_dir) / "state.json"
            save_state(state_path, {})

            def fetcher(page: int) -> tuple[list[dict[str, Any]], int]:
                payload = self.page_1 if page == 1 else self.page_2
                return parse_page_response(payload)

            def image_fetcher(_: str) -> bytes | None:
                return None

            def sender(message: EmailMessage, _: MailConfig) -> None:
                sent_messages.append(message["Subject"])

            sent_count = process_watch(
                config=self.config,
                state_path=state_path,
                fetcher=fetcher,
                image_fetcher=image_fetcher,
                sender=sender,
                now=datetime(2026, 4, 21, tzinfo=UTC),
            )

            state = load_state(state_path)
            self.assertEqual(sent_count, 2)
            self.assertEqual(len(sent_messages), 1)
            self.assertEqual(sorted(state.keys()), ["480695", "481670"])

    def test_main_returns_zero_after_successful_run(self) -> None:
        with (
            patch("scripts.uniqlo_price_watch.load_mail_config_from_env", return_value=self.config),
            patch("scripts.uniqlo_price_watch.process_watch", return_value=4),
        ):
            self.assertEqual(main(), 0)


if __name__ == "__main__":
    unittest.main()
