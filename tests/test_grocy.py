#!/usr/bin/python3 -I
"""Helper tests. They use a loopback fake Grocy and never a real API key."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import threading
import unittest
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

HELPER = Path(__file__).resolve().parents[1] / "bin" / "grocy.py"
SPEC = importlib.util.spec_from_file_location("jordan_grocy", HELPER)
assert SPEC and SPEC.loader
grocy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(grocy)

ZONE = ZoneInfo("UTC")
NOW = datetime(2026, 10, 11, 15, tzinfo=ZONE)
NAME = "Order 2026-W43"
KEY = "ab" * 16


class Box:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.lists: list[dict] = []
        self.lines: list[dict] = []
        self.products: list[dict] = []
        self.calls: list[tuple] = []
        self.added: list[dict] = []
        self.stock: list = []
        self.add_status = 204
        self.add_error = "nope"
        self.remove_status = 204
        self.remove_error = "nope"
        self.line_status = 200
        self.line_error = "nope"
        self.plant = False
        self.redirect = False
        self.oversize = False
        self.create_status = 200


BOX = Box()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, status: int, payload: object) -> None:
        if payload is None:
            body = b""
        else:
            body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _read(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 10000:
            return self.rfile.read(10000)
        return self.rfile.read(length) if length else b""

    def _record(self, body: bytes) -> None:
        with BOX.lock:
            BOX.calls.append((self.command, self.path, body, self.headers.get("GROCY-API-KEY")))

    def do_GET(self) -> None:
        self._record(b"")
        with BOX.lock:
            if BOX.redirect:
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1/stolen")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            if BOX.oversize and self.path == "/api/stock":
                body = b"x" * (grocy.MAX_BODY + 2)
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/api/objects/shopping_lists":
                payload = list(BOX.lists)
            elif self.path == "/api/objects/shopping_list":
                payload = list(BOX.lines)
            elif self.path == "/api/objects/products":
                payload = list(BOX.products)
            elif self.path == "/api/stock":
                payload = list(BOX.stock)
            else:
                self._send(404, {"error": "missing"})
                return
        self._send(200, payload)

    def do_POST(self) -> None:
        body = self._read()
        self._record(body)
        with BOX.lock:
            if self.path == "/api/objects/shopping_lists":
                if BOX.create_status != 200:
                    self._send(BOX.create_status, {"error": "create failed"})
                    return
                if BOX.plant:
                    BOX.lists = [{"id": 2, "name": NAME}, {"id": 9, "name": NAME}]
                    self._send(200, {"created_object_id": "9"})
                    return
                parsed = json.loads(body.decode())
                new_id = max((row["id"] for row in BOX.lists), default=0) + 1
                BOX.lists.append({"id": new_id, "name": parsed["name"]})
                self._send(200, {"created_object_id": str(new_id)})
                return
            if self.path == "/api/objects/shopping_list":
                if BOX.line_status not in {200, 201}:
                    self._send(BOX.line_status, {"error": BOX.line_error})
                    return
                parsed = json.loads(body.decode())
                new_id = max((row["id"] for row in BOX.lines), default=0) + 1
                BOX.lines.append({
                    "id": new_id,
                    "product_id": None,
                    "shopping_list_id": parsed["shopping_list_id"],
                    "amount": parsed["amount"],
                    "note": parsed["note"],
                })
                self._send(200, {"created_object_id": str(new_id)})
                return
            if self.path == "/api/stock/shoppinglist/add-product":
                if BOX.add_status != 204:
                    self._send(BOX.add_status, {"error": BOX.add_error})
                    return
                BOX.added.append(json.loads(body.decode()))
                self._send(204, None)
                return
        self._send(404, {"error": "missing"})

    def do_PUT(self) -> None:
        body = self._read()
        self._record(body)
        prefix = "/api/objects/shopping_list/"
        with BOX.lock:
            if not self.path.startswith(prefix):
                self._send(404, {"error": "missing"})
                return
            line_id = int(self.path[len(prefix):])
            parsed = json.loads(body.decode())
            for row in BOX.lines:
                if row["id"] == line_id:
                    row["amount"] = parsed["amount"]
                    self._send(200, {"id": line_id})
                    return
            self._send(404, {"error": "missing"})

    def do_DELETE(self) -> None:
        self._record(b"")
        with BOX.lock:
            lists_prefix = "/api/objects/shopping_lists/"
            line_prefix = "/api/objects/shopping_list/"
            if self.path.startswith(lists_prefix):
                list_id = int(self.path[len(lists_prefix):])
                BOX.lists = [row for row in BOX.lists if row["id"] != list_id]
                self._send(204, None)
                return
            if self.path.startswith(line_prefix):
                if BOX.remove_status not in {200, 204}:
                    self._send(BOX.remove_status, {"error": BOX.remove_error})
                    return
                line_id = int(self.path[len(line_prefix):])
                kept = [row for row in BOX.lines if row["id"] != line_id]
                if len(kept) == len(BOX.lines):
                    self._send(404, {"error": "missing"})
                    return
                BOX.lines = kept
                self._send(204, None)
                return
        self._send(404, {"error": "missing"})


class Server:
    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=3)
        self.httpd.server_close()


def stock_row(product_id: int, name: str, amount: object = 1) -> dict:
    return {"product_id": product_id, "amount": amount, "product": {"id": product_id, "name": name}}


class GrocyHelperTest(unittest.TestCase):
    def setUp(self) -> None:
        BOX.lists = []
        BOX.lines = []
        BOX.products = []
        BOX.calls = []
        BOX.added = []
        BOX.stock = []
        BOX.add_status = 204
        BOX.add_error = "nope"
        BOX.remove_status = 204
        BOX.remove_error = "nope"
        BOX.line_status = 200
        BOX.line_error = "nope"
        BOX.plant = False
        BOX.redirect = False
        BOX.oversize = False
        BOX.create_status = 200
        self.server = Server()
        self.home = self.fresh_home()

    def tearDown(self) -> None:
        self.server.close()

    def fresh_home(self) -> str:
        import tempfile
        home = tempfile.mkdtemp(prefix="grocy-home-")
        os.chmod(home, 0o755)
        return home

    def add(self, product_id: int = 2) -> dict:
        return grocy.add_product(
            self.server.origin,
            KEY,
            product_id,
            home=self.home,
            allow_loopback=True,
            now=NOW,
        )

    def remove(self, product_id: int = 4) -> dict:
        return grocy.remove_product(
            self.server.origin,
            KEY,
            product_id,
            home=self.home,
            allow_loopback=True,
            now=NOW,
        )

    def posts(self, path: str) -> list[tuple]:
        return [call for call in BOX.calls if call[0] == "POST" and call[1] == path]

    def test_week_names(self) -> None:
        cases = {
            "2026-10-11T15:00:00": "Order 2026-W43",
            "2026-10-12T00:00:00": "Order 2026-W43",
            "2026-10-13T08:00:00": "Order 2026-W43",
            "2026-10-17T12:00:00": "Order 2026-W43",
            "2026-10-18T12:00:00": "Order 2026-W44",
            "2026-12-27T12:00:00": "Order 2027-W01",
            "2026-12-28T12:00:00": "Order 2027-W01",
            "2027-01-03T12:00:00": "Order 2027-W02",
        }
        for text, expected in cases.items():
            moment = datetime.fromisoformat(text).replace(tzinfo=ZONE)
            self.assertEqual(grocy.next_order_list_name(moment), expected)

    def test_stock_parse_sorts_and_keeps_amount(self) -> None:
        items = grocy.parse_stock([
            stock_row(3, "Eggs", "2"),
            stock_row(2, "Milk", 1.5),
        ])
        self.assertEqual(items, [
            {"id": "3", "name": "Eggs", "amount": "2"},
            {"id": "2", "name": "Milk", "amount": "1.5"},
        ])

    def test_stock_rejects_the_whole_document(self) -> None:
        too_many = [stock_row(i, f"Item {i}") for i in range(1, 402)]
        with self.assertRaises(grocy.GrocyError):
            grocy.parse_stock(too_many)
        with self.assertRaises(grocy.GrocyError):
            grocy.parse_stock([stock_row(2, "Milk"), stock_row(2, "Milk again")])
        with self.assertRaises(grocy.GrocyError):
            grocy.parse_stock([stock_row(2, "Milk", float("inf"))])
        with self.assertRaises(grocy.GrocyError):
            grocy.parse_stock({"items": []})

    def test_reuses_named_list_and_ignores_the_default(self) -> None:
        BOX.lists = [{"id": 1, "name": "Shopping list"}, {"id": 7, "name": NAME}]
        result = self.add(4)
        self.assertEqual(result["list_id"], 7)
        self.assertFalse(result["created"])
        self.assertEqual(self.posts("/api/objects/shopping_lists"), [])
        self.assertEqual(BOX.added, [{"product_id": 4, "list_id": 7, "product_amount": 1}])

    def test_creates_once_then_reuses(self) -> None:
        BOX.lists = [{"id": 1, "name": "Shopping list"}]
        first = self.add(4)
        second = self.add(5)
        creates = self.posts("/api/objects/shopping_lists")
        self.assertEqual(len(creates), 1)
        self.assertEqual(first["list_id"], second["list_id"])
        self.assertNotEqual(first["list_id"], 1)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual([row["list_id"] for row in BOX.added], [first["list_id"], first["list_id"]])
        self.assertEqual(len([row for row in BOX.lists if row["name"] == NAME]), 1)

    def test_two_existing_lists_use_the_oldest_and_do_not_create(self) -> None:
        BOX.lists = [{"id": 8, "name": NAME}, {"id": 4, "name": NAME}]
        result = self.add(3)
        self.assertEqual(result["list_id"], 4)
        self.assertEqual(self.posts("/api/objects/shopping_lists"), [])
        self.assertFalse(any(call[0] == "DELETE" for call in BOX.calls))

    def test_extra_list_from_a_race_is_removed(self) -> None:
        BOX.plant = True
        result = self.add(6)
        self.assertEqual(result["list_id"], 2)
        self.assertFalse(result["created"])
        self.assertEqual(len(self.posts("/api/objects/shopping_lists")), 1)
        self.assertIn(("DELETE", "/api/objects/shopping_lists/9", b"", KEY), BOX.calls)
        self.assertEqual(BOX.added[0]["list_id"], 2)
        self.assertEqual([row["id"] for row in BOX.lists], [2])

    def test_add_error_does_not_create_another_list(self) -> None:
        BOX.lists = [{"id": 3, "name": NAME}]
        BOX.add_status = 400
        BOX.add_error = f"nope {KEY}"
        with self.assertRaises(grocy.GrocyError) as caught:
            self.add(4)
        self.assertIn("400", str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))
        self.assertEqual(self.posts("/api/objects/shopping_lists"), [])
        self.assertEqual(BOX.lists, [{"id": 3, "name": NAME}])

    def test_redirect_is_refused(self) -> None:
        BOX.redirect = True
        with self.assertRaises(grocy.GrocyError) as caught:
            grocy.fetch_stock(self.server.origin, KEY, allow_loopback=True, now=NOW)
        self.assertIn("redirect", str(caught.exception))
        self.assertEqual(len(BOX.calls), 1)

    def test_oversize_response_is_refused(self) -> None:
        BOX.oversize = True
        with self.assertRaises(grocy.GrocyError):
            grocy.fetch_stock(self.server.origin, KEY, allow_loopback=True, now=NOW)

    def test_key_stays_out_of_the_url_and_body(self) -> None:
        BOX.lists = [{"id": 3, "name": NAME}]
        self.add(4)
        for method, path, body, header in BOX.calls:
            self.assertEqual(header, KEY)
            self.assertNotIn(KEY, path)
            self.assertNotIn(KEY.encode(), body)

    def test_parallel_adds_create_one_list(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self.add, 4),
                pool.submit(self.add, 5),
            ]
            results = [future.result() for future in futures]
        self.assertEqual(len(self.posts("/api/objects/shopping_lists")), 1)
        self.assertEqual(results[0]["list_id"], results[1]["list_id"])
        self.assertEqual(len([row for row in BOX.lists if row["name"] == NAME]), 1)

    def test_credentials_reject_a_loose_or_wrong_file(self) -> None:
        home = Path(self.home)
        config = home / ".config" / "jordan-grocy"
        config.mkdir(parents=True, mode=0o755)
        target = config / "config.env"
        origin = "https://grocy.example"
        target.write_text(f"GROCY_URL=http://evil.example\nGROCY_API_KEY={KEY}\n")
        os.chmod(target, 0o600)
        with self.assertRaises(grocy.GrocyError) as caught:
            grocy.load_credentials(self.home)
        self.assertNotIn(KEY, str(caught.exception))
        target.write_text(f"GROCY_URL={origin}\nGROCY_API_KEY={KEY}\n")
        os.chmod(target, 0o644)
        with self.assertRaises(grocy.GrocyError):
            grocy.load_credentials(self.home)
        os.chmod(target, 0o600)
        self.assertEqual(grocy.load_credentials(self.home), (origin, KEY))
        self.assertEqual(grocy.parse_timezone(target.read_text()), ZONE)
        target.write_text(f"GROCY_URL={origin}\nGROCY_API_KEY={KEY}\nGROCY_TIMEZONE=Not/AZone\n")
        with self.assertRaises(grocy.GrocyError):
            grocy.parse_timezone(target.read_text())
        target.write_text(f"GROCY_URL={origin}\nGROCY_API_KEY={KEY}\n")
        swapped = config / "real.env"
        swapped.write_text(target.read_text())
        os.chmod(swapped, 0o600)
        target.unlink()
        target.symlink_to(swapped)
        with self.assertRaises(grocy.GrocyError):
            grocy.load_credentials(self.home)

    def test_tailnet_addresses(self) -> None:
        self.assertTrue(grocy.tailnet_ip("100.64.1.2"))
        self.assertTrue(grocy.tailnet_ip("fd7a:115c:a1e0::1"))
        self.assertFalse(grocy.tailnet_ip("127.0.0.1"))
        self.assertFalse(grocy.tailnet_ip("10.1.2.3"))
        self.assertFalse(grocy.tailnet_ip("not-an-ip"))
        self.assertTrue(grocy.allowed_server_ip("100.64.1.2"))
        self.assertTrue(grocy.allowed_server_ip("10.1.2.3"))
        self.assertTrue(grocy.allowed_server_ip("8.8.8.8"))
        self.assertFalse(grocy.allowed_server_ip("127.0.0.1"))
        self.assertFalse(grocy.allowed_server_ip("169.254.169.254"))
        self.assertFalse(grocy.valid_https_origin("http://grocy.example"))
        self.assertFalse(grocy.valid_https_origin("https://user:pw@grocy.example"))
        self.assertTrue(grocy.valid_https_origin("https://grocy.example"))

    def test_list_name_command(self) -> None:
        completed = subprocess.run(
            ["/usr/bin/python3", "-I", "-S", str(HELPER), "list-name"],
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin", "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        doc = json.loads(completed.stdout)
        self.assertTrue(doc["ok"])
        self.assertRegex(doc["list"], r"^Order [0-9]{4}-W[0-9]{2}$")

    def test_remove_takes_every_line_for_that_product(self) -> None:
        BOX.lists = [{"id": 1, "name": "Shopping list"}, {"id": 7, "name": NAME}]
        BOX.lines = [
            {"id": 10, "product_id": 4, "shopping_list_id": 1, "amount": 1},
            {"id": 11, "product_id": 4, "shopping_list_id": 7, "amount": 1},
            {"id": 12, "product_id": 4, "shopping_list_id": 7, "amount": 2},
        ]
        result = self.remove(4)
        self.assertEqual(result["list_id"], 7)
        self.assertEqual(result["removed"], 2)
        self.assertFalse(result.get("created", False))
        self.assertEqual(self.posts("/api/objects/shopping_lists"), [])
        self.assertFalse(any(call[0] == "POST" for call in BOX.calls))
        self.assertEqual([row["id"] for row in BOX.lines], [10])
        self.assertEqual(BOX.lists, [{"id": 1, "name": "Shopping list"}, {"id": 7, "name": NAME}])

    def test_remove_when_the_named_list_is_missing(self) -> None:
        BOX.lists = [{"id": 1, "name": "Shopping list"}]
        BOX.lines = [{"id": 10, "product_id": 4, "shopping_list_id": 1, "amount": 1}]
        with self.assertRaises(grocy.GrocyError) as caught:
            self.remove(4)
        self.assertIn("not on the order", str(caught.exception))
        self.assertEqual(self.posts("/api/objects/shopping_lists"), [])
        self.assertFalse(any(call[0] == "DELETE" for call in BOX.calls))
        self.assertEqual([row["id"] for row in BOX.lines], [10])

    def test_remove_ignores_a_newer_list_with_the_same_name(self) -> None:
        BOX.lists = [{"id": 9, "name": NAME}, {"id": 4, "name": NAME}]
        BOX.lines = [{"id": 11, "product_id": 4, "shopping_list_id": 9, "amount": 1}]
        with self.assertRaises(grocy.GrocyError) as caught:
            self.remove(4)
        self.assertIn("not on the order", str(caught.exception))
        self.assertFalse(any(call[0] == "DELETE" for call in BOX.calls))
        self.assertEqual([row["id"] for row in BOX.lines], [11])

    def test_remove_error_keeps_the_line_and_redacts_the_key(self) -> None:
        BOX.lists = [{"id": 7, "name": NAME}]
        BOX.lines = [{"id": 11, "product_id": 4, "shopping_list_id": 7, "amount": 1}]
        BOX.remove_status = 400
        BOX.remove_error = f"nope {KEY}"
        with self.assertRaises(grocy.GrocyError) as caught:
            self.remove(4)
        self.assertIn("400", str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))
        self.assertEqual([row["id"] for row in BOX.lines], [11])
        self.assertEqual(BOX.lists, [{"id": 7, "name": NAME}])

    def test_remove_stops_before_delete_when_one_item_has_too_many_lines(self) -> None:
        BOX.lists = [{"id": 7, "name": NAME}]
        BOX.lines = [
            {"id": i, "product_id": 4, "shopping_list_id": 7, "amount": 1}
            for i in range(1, 42)
        ]
        with self.assertRaises(grocy.GrocyError) as caught:
            self.remove(4)
        self.assertIn("too many lines", str(caught.exception))
        self.assertFalse(any(call[0] == "DELETE" for call in BOX.calls))
        self.assertEqual(len(BOX.lines), 41)

    def test_remove_keeps_the_key_out_of_the_url_and_body(self) -> None:
        BOX.lists = [{"id": 7, "name": NAME}]
        BOX.lines = [{"id": 11, "product_id": 4, "shopping_list_id": 7, "amount": 1}]
        self.remove(4)
        deletes = [call for call in BOX.calls if call[0] == "DELETE"]
        self.assertEqual([call[1] for call in deletes], ["/api/objects/shopping_list/11"])
        for method, path, body, header in BOX.calls:
            self.assertEqual(header, KEY)
            self.assertNotIn(KEY, path)
            self.assertNotIn(KEY.encode(), body)

    def test_stock_includes_the_named_order(self) -> None:
        BOX.stock = [stock_row(18, "Bananas", 1)]
        BOX.lists = [{"id": 1, "name": "Shopping list"}, {"id": 3, "name": NAME}]
        BOX.lines = [
            {"id": 3, "product_id": 18, "shopping_list_id": 3, "amount": "1.5"},
            {"id": 4, "product_id": 18, "shopping_list_id": 3, "amount": 1.5},
            {"id": 5, "product_id": 2, "shopping_list_id": 1, "amount": 9},
        ]
        BOX.products = [{"id": 18, "name": "Bananas, Bunch"}, {"id": 2, "name": "Milk"}]
        doc = grocy.fetch_stock(self.server.origin, KEY, allow_loopback=True, now=NOW)
        self.assertEqual(doc["list"], NAME)
        self.assertEqual(doc["items"], [{"id": "18", "name": "Bananas", "amount": "1"}])
        self.assertEqual(doc["order"], [{"id": "18", "name": "Bananas, Bunch", "amount": "3"}])
        self.assertTrue(any(call[1] == "/api/objects/products" for call in BOX.calls))

    def test_stock_order_is_empty_without_creating_a_list(self) -> None:
        BOX.stock = [stock_row(2, "Milk", 1)]
        BOX.lists = [{"id": 1, "name": "Shopping list"}]
        doc = grocy.fetch_stock(self.server.origin, KEY, allow_loopback=True, now=NOW)
        self.assertEqual(doc["order"], [])
        self.assertEqual(self.posts("/api/objects/shopping_lists"), [])
        self.assertFalse(any(call[1] == "/api/objects/products" for call in BOX.calls))
        self.assertEqual(BOX.lists, [{"id": 1, "name": "Shopping list"}])

    def test_missing_product_name_falls_back(self) -> None:
        items = grocy.group_order([{"product_id": 9, "amount": 1.0}], {})
        self.assertEqual(items, [{"id": "9", "name": "Product 9", "amount": "1"}])

    def test_a_store_note_keeps_the_product_already_on_the_list(self) -> None:
        BOX.stock = [stock_row(18, "Bananas", 2)]
        BOX.lists = [{"id": 1, "name": "Shopping list"}, {"id": 7, "name": NAME}]
        BOX.lines = [
            {"id": 3, "product_id": 18, "shopping_list_id": 7, "amount": 2},
            {"id": 4, "product_id": None, "shopping_list_id": 7, "amount": 1, "note": "bring a bag"},
            {
                "id": 5,
                "product_id": None,
                "shopping_list_id": 7,
                "amount": 1,
                "note": "store|4.49|3.99|Simple Truth Milk",
            },
        ]
        BOX.products = [{"id": 18, "name": "Bananas, Bunch"}]
        doc = grocy.fetch_stock(self.server.origin, KEY, allow_loopback=True, now=NOW)
        self.assertEqual(doc["order"][0], {"id": "18", "name": "Bananas, Bunch", "amount": "2"})
        self.assertEqual(doc["order"][1], {
            "id": "s5",
            "name": "Simple Truth Milk",
            "amount": "1",
            "price": "4.49",
            "loyalty": "3.99",
            "krogerId": "",
            "upc": "",
        })


class KrogerBox:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[tuple] = []
        self.products: list[dict] = []
        self.details: dict[str, dict] = {}
        self.locations: list[dict] = []
        self.location = "10000002"
        self.token = "t" * 24
        self.token_status = 200
        self.token_error = "nope"
        self.search_status = 200
        self.detail_status = 200
        self.location_status = 200
        self.redirect = False
        self.oversize = False


KBOX = KrogerBox()


def kroger_product(product_id: str, name: str, regular: object, promo: object | None = None, loyalty: object | None = None, upc: str | None = None) -> dict:
    price: dict[str, object] = {"regular": regular}
    if promo is not None:
        price["promo"] = promo
    if loyalty is not None:
        price["loyalty"] = loyalty
    item: dict[str, object] = {"itemId": product_id, "price": price}
    if upc is not None:
        item["upc"] = upc
    return {"productId": product_id, "description": name, "items": [item]}


class KrogerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _auth_kind(self) -> str:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            return "basic"
        if auth.startswith("Bearer "):
            return "bearer"
        return "none"

    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _record(self) -> None:
        with KBOX.lock:
            KBOX.calls.append((self.command, self.path, self._auth_kind()))

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(min(length, 1000))
        self._record()
        with KBOX.lock:
            if KBOX.redirect:
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1/stolen")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            if self.path != "/v1/connect/oauth2/token" or self._auth_kind() != "basic":
                self._send(404, {"error": "missing"})
                return
            if KBOX.token_status != 200:
                self._send(KBOX.token_status, {"error": KBOX.token_error})
                return
            self._send(200, {"access_token": KBOX.token, "expires_in": 1800, "token_type": "bearer"})

    def do_GET(self) -> None:
        self._record()
        parsed = urllib.parse.urlsplit(self.path)
        with KBOX.lock:
            if KBOX.redirect:
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1/stolen")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            if self._auth_kind() != "bearer":
                self._send(401, {"error": "no token"})
                return
            if KBOX.oversize and parsed.path == "/v1/products":
                body = b"x" * (grocy.MAX_BODY + 2)
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/v1/locations":
                query = urllib.parse.parse_qs(parsed.query)
                chain = (query.get("filter.chain") or [""])[0]
                try:
                    limit = int((query.get("filter.limit") or ["8"])[0])
                except ValueError:
                    limit = 8
                rows = list(KBOX.locations)
                if chain:
                    rows = [row for row in rows if row.get("chain") == chain]
                self._send(200, {"data": rows[: max(0, limit)]})
                return
            location_prefix = "/v1/locations/"
            if parsed.path.startswith(location_prefix):
                if KBOX.location_status != 200:
                    self._send(KBOX.location_status, {"error": "nope"})
                    return
                wanted = parsed.path[len(location_prefix):]
                found = next((row for row in KBOX.locations if row.get("locationId") == wanted), None)
                if found is None:
                    self._send(404, {"error": "missing"})
                    return
                self._send(200, {"data": found})
                return
            if parsed.path == "/v1/products":
                if KBOX.search_status != 200:
                    self._send(KBOX.search_status, {"error": "nope"})
                    return
                query = urllib.parse.parse_qs(parsed.query)
                term = (query.get("filter.term") or [""])[0].casefold()
                rows = [row for row in KBOX.products if term in str(row.get("description", "")).casefold()]
                try:
                    start = int((query.get("filter.start") or ["0"])[0])
                    limit = int((query.get("filter.limit") or ["24"])[0])
                except ValueError:
                    start, limit = 0, 24
                page = rows[max(0, start): max(0, start) + max(0, limit)]
                self._send(200, {
                    "data": page,
                    "meta": {"pagination": {"start": max(0, start), "limit": max(0, limit), "total": len(rows)}},
                })
                return
            prefix = "/v1/products/"
            if parsed.path.startswith(prefix):
                if KBOX.detail_status != 200:
                    self._send(KBOX.detail_status, {"error": "nope"})
                    return
                product = KBOX.details.get(parsed.path[len(prefix):])
                if product is None:
                    self._send(404, {"error": "missing"})
                    return
                self._send(200, {"data": product})
                return
        self._send(404, {"error": "missing"})


class KrogerServer:
    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), KrogerHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=3)
        self.httpd.server_close()


class KrogerStoreTest(GrocyHelperTest):
    def setUp(self) -> None:
        super().setUp()
        KBOX.calls = []
        KBOX.products = []
        KBOX.details = {}
        KBOX.locations = []
        KBOX.token_status = 200
        KBOX.token_error = "nope"
        KBOX.search_status = 200
        KBOX.detail_status = 200
        KBOX.location_status = 200
        KBOX.redirect = False
        KBOX.oversize = False
        self.kroger = KrogerServer()
        self.client = "clientid12"
        self.secret = "secretsecretsecret"
        self.location = "10000002"
        self.milk = "0001111042850"
        self.plain = "0001111042010"

    def tearDown(self) -> None:
        self.kroger.close()
        super().tearDown()

    def search(self, term: str = "milk", start: int = 0) -> dict:
        return grocy.search_store(
            self.kroger.origin,
            self.client,
            self.secret,
            self.location,
            term,
            home=self.home,
            allow_loopback=True,
            start=start,
        )

    def add_hit(self, product_id: str | None = None) -> dict:
        return grocy.add_store_hit(
            self.server.origin,
            KEY,
            self.kroger.origin,
            self.client,
            self.secret,
            self.location,
            product_id or self.milk,
            home=self.home,
            allow_loopback=True,
            now=NOW,
        )

    def plant_catalog(self) -> None:
        complete = kroger_product(self.milk, "Simple Truth Milk", 4.49, promo=3.99)
        missing = kroger_product(self.plain, "Plain Milk", 2.49)
        both = kroger_product("0001111050434", "Card Milk", 5.00, promo=1.00, loyalty=2.50)
        KBOX.products = [complete, missing, both]
        KBOX.details = {row["productId"]: row for row in KBOX.products}

    def test_search_shows_name_price_and_loyalty_price(self) -> None:
        self.plant_catalog()
        doc = self.search("milk")
        by_id = {hit["id"]: hit for hit in doc["hits"]}
        self.assertEqual(by_id[self.milk]["name"], "Simple Truth Milk")
        self.assertEqual(by_id[self.milk]["price"], "4.49")
        self.assertEqual(by_id[self.milk]["loyalty"], "3.99")
        self.assertTrue(by_id[self.milk]["complete"])
        self.assertEqual(by_id[self.plain]["price"], "2.49")
        self.assertEqual(by_id[self.plain]["loyalty"], "")
        self.assertTrue(by_id[self.plain]["complete"])
        self.assertEqual(by_id["0001111050434"]["price"], "5.00")
        self.assertEqual(by_id["0001111050434"]["loyalty"], "1.00")
        self.assertTrue(by_id["0001111050434"]["complete"])
        self.assertEqual(doc["store"], self.location)
        self.assertTrue(any(call[1].startswith("/v1/products?") and f"filter.locationId={self.location}" in call[1] for call in KBOX.calls))
        self.assertEqual(BOX.calls, [])

    def test_search_reuses_a_token_and_keeps_secrets_out_of_the_url(self) -> None:
        self.plant_catalog()
        self.search("milk")
        self.search("milk")
        posts = [call for call in KBOX.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2], "basic")
        for method, path, kind in KBOX.calls:
            self.assertNotIn(self.secret, path)
            self.assertNotIn(self.client, path)
            self.assertNotIn(KBOX.token, path)
            self.assertIn(kind, {"basic", "bearer"})

    def test_search_error_does_not_touch_the_grocery_list(self) -> None:
        BOX.lists = [{"id": 7, "name": NAME}]
        BOX.lines = [{"id": 3, "product_id": 18, "shopping_list_id": 7, "amount": 2}]
        KBOX.search_status = 500
        with self.assertRaises(grocy.GrocyError) as caught:
            self.search("milk")
        self.assertIn("500", str(caught.exception))
        self.assertNotIn(self.secret, str(caught.exception))
        self.assertEqual(BOX.calls, [])
        self.assertEqual(BOX.lines, [{"id": 3, "product_id": 18, "shopping_list_id": 7, "amount": 2}])
        self.assertEqual(BOX.lists, [{"id": 7, "name": NAME}])

    def test_redirect_and_oversize_are_refused(self) -> None:
        KBOX.redirect = True
        with self.assertRaises(grocy.GrocyError) as caught:
            self.search("milk")
        self.assertIn("redirect", str(caught.exception))
        KBOX.redirect = False
        KBOX.oversize = True
        with self.assertRaises(grocy.GrocyError):
            self.search("eggs")
        self.assertEqual(BOX.calls, [])

    def test_bad_term_does_not_call_kroger(self) -> None:
        with self.assertRaises(grocy.GrocyError):
            self.search("-milk")
        with self.assertRaises(grocy.GrocyError):
            self.search("")
        with self.assertRaises(grocy.GrocyError) as short:
            self.search("oj")
        self.assertIn("3", str(short.exception))
        self.assertEqual(KBOX.calls, [])
        self.assertEqual(BOX.calls, [])

    def test_kroger_error_names_kroger_and_the_reason(self) -> None:
        blob = json.dumps({
            "errors": {
                "code": "PRODUCT-2017",
                "reason": "Field 'term' must have three or more characters",
            }
        }).encode()
        text = grocy._kroger_error(400, blob, (self.secret,))
        self.assertIn("Kroger returned 400", text)
        self.assertIn("three or more", text)
        self.assertNotIn("Grocy", text)
        self.assertNotIn(self.secret, text)

    def test_token_error_redacts_the_secret(self) -> None:
        KBOX.token_status = 400
        KBOX.token_error = f"nope {self.secret}"
        with self.assertRaises(grocy.GrocyError) as caught:
            self.search("milk")
        self.assertIn("400", str(caught.exception))
        self.assertNotIn(self.secret, str(caught.exception))
        self.assertEqual(BOX.calls, [])

    def test_promo_zero_is_no_sale_and_other_price_keys_are_ignored(self) -> None:
        zero = grocy.hit_from_product(kroger_product("0001111050101", "Avocado", 1.25, promo=0))
        member = grocy.hit_from_product(kroger_product("0001111050102", "Member Milk", 4.00, loyalty=2.00))
        self.assertEqual(zero["price"], "1.25")
        self.assertEqual(zero["loyalty"], "")
        self.assertTrue(zero["complete"])
        self.assertEqual(member["price"], "4.00")
        self.assertEqual(member["loyalty"], "")
        self.assertTrue(member["complete"])
        KBOX.products = [kroger_product("0001111050101", "Avocado", 1.25, promo=0)]
        KBOX.details = {row["productId"]: row for row in KBOX.products}
        BOX.lists = [{"id": 7, "name": NAME}]
        added = self.add_hit("0001111050101")
        self.assertEqual(added["krogerId"], "0001111050101")
        self.assertEqual(added["upc"], "")
        note = json.loads([call for call in BOX.calls if call[1] == "/api/objects/shopping_list"][0][2])["note"]
        self.assertEqual(note, "store|1.25|-|0001111050101|-|Avocado")
        self.assertTrue(all("cart" not in call[1].lower() for call in KBOX.calls))

    def test_store_calls_never_touch_a_cart(self) -> None:
        self.plant_catalog()
        BOX.lists = [{"id": 7, "name": NAME}]
        self.search("milk")
        self.add_hit(self.milk)
        paths = [call[1] for call in KBOX.calls] + [call[1] for call in BOX.calls]
        self.assertTrue(paths)
        self.assertTrue(all("cart" not in path.lower() for path in paths))
        with self.assertRaises(grocy.GrocyError) as caught:
            grocy.kroger_exchange(
                "http://127.0.0.1:9",
                "GET",
                "/v1/cart/add",
                bearer="t" * 24,
                allow_loopback=True,
            )
        self.assertIn("refusing path", str(caught.exception))

    def test_incomplete_hit_is_not_added(self) -> None:
        self.plant_catalog()
        BOX.lists = [{"id": 7, "name": NAME}]
        BOX.lines = [{"id": 3, "product_id": 18, "shopping_list_id": 7, "amount": 2}]
        KBOX.details[self.plain] = kroger_product(self.plain, "Plain Milk", 0)
        before = list(BOX.lines)
        with self.assertRaises(grocy.GrocyError) as caught:
            self.add_hit(self.plain)
        self.assertIn("price", str(caught.exception))
        self.assertEqual(BOX.lines, before)
        self.assertEqual(BOX.lists, [{"id": 7, "name": NAME}])
        self.assertFalse(any(call[0] == "POST" for call in BOX.calls))

    def test_shelf_price_is_added_with_the_product_id_and_upc(self) -> None:
        self.plant_catalog()
        KBOX.details[self.plain] = kroger_product(self.plain, "Plain Milk", 2.49, upc="0001111042011")
        BOX.lists = [{"id": 7, "name": NAME}]
        added = self.add_hit(self.plain)
        self.assertEqual(added["amount"], 1)
        self.assertEqual(added["upc"], "0001111042011")
        note = json.loads([call for call in BOX.calls if call[1] == "/api/objects/shopping_list"][0][2])["note"]
        self.assertEqual(note, "store|2.49|-|0001111042010|0001111042011|Plain Milk")
        doc = grocy.fetch_stock(self.server.origin, KEY, allow_loopback=True, now=NOW)
        self.assertEqual(doc["order"][0]["krogerId"], self.plain)
        self.assertEqual(doc["order"][0]["upc"], "0001111042011")
        self.assertEqual(doc["order"][0]["loyalty"], "")

    def test_kroger_error_on_add_leaves_the_list(self) -> None:
        self.plant_catalog()
        BOX.lists = [{"id": 7, "name": NAME}]
        BOX.lines = [{"id": 3, "product_id": 18, "shopping_list_id": 7, "amount": 2}]
        KBOX.detail_status = 502
        with self.assertRaises(grocy.GrocyError):
            self.add_hit(self.milk)
        self.assertEqual(BOX.lines, [{"id": 3, "product_id": 18, "shopping_list_id": 7, "amount": 2}])
        self.assertFalse(any(call[0] == "POST" for call in BOX.calls))

    def test_add_appends_and_the_old_row_still_loads(self) -> None:
        self.plant_catalog()
        BOX.stock = [stock_row(18, "Bananas", 2)]
        BOX.lists = [{"id": 1, "name": "Shopping list"}, {"id": 7, "name": NAME}]
        BOX.lines = [{"id": 3, "product_id": 18, "shopping_list_id": 7, "amount": 2}]
        BOX.products = [{"id": 18, "name": "Bananas, Bunch"}]
        added = self.add_hit(self.milk)
        self.assertEqual(added["list"], NAME)
        self.assertEqual(added["list_id"], 7)
        self.assertFalse(added["created"])
        doc = grocy.fetch_stock(self.server.origin, KEY, allow_loopback=True, now=NOW)
        self.assertEqual(doc["items"], [{"id": "18", "name": "Bananas", "amount": "2"}])
        self.assertEqual(doc["order"][0], {"id": "18", "name": "Bananas, Bunch", "amount": "2"})
        self.assertEqual(doc["order"][1]["name"], "Simple Truth Milk")
        self.assertEqual(doc["order"][1]["price"], "4.49")
        self.assertEqual(doc["order"][1]["loyalty"], "3.99")
        note = json.loads([call for call in BOX.calls if call[1] == "/api/objects/shopping_list"][0][2])["note"]
        self.assertEqual(note, "store|4.49|3.99|0001111042850|-|Simple Truth Milk")
        self.assertNotIn(self.secret, note)
        self.assertNotIn(KEY, note)

    def test_grocy_line_error_keeps_an_existing_list(self) -> None:
        self.plant_catalog()
        BOX.lists = [{"id": 7, "name": NAME}]
        BOX.lines = [{"id": 3, "product_id": 18, "shopping_list_id": 7, "amount": 2}]
        BOX.line_status = 400
        BOX.line_error = f"nope {KEY}"
        with self.assertRaises(grocy.GrocyError) as caught:
            self.add_hit(self.milk)
        self.assertIn("400", str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))
        self.assertEqual(BOX.lines, [{"id": 3, "product_id": 18, "shopping_list_id": 7, "amount": 2}])
        self.assertEqual(BOX.lists, [{"id": 7, "name": NAME}])

    def test_grocy_line_error_removes_a_list_this_add_created(self) -> None:
        self.plant_catalog()
        BOX.lists = [{"id": 1, "name": "Shopping list"}]
        BOX.line_status = 400
        with self.assertRaises(grocy.GrocyError):
            self.add_hit(self.milk)
        self.assertEqual(BOX.lists, [{"id": 1, "name": "Shopping list"}])
        self.assertEqual(BOX.lines, [])

    def test_kroger_file_must_be_private(self) -> None:
        home = Path(self.home)
        config = home / ".config" / "jordan-grocy"
        config.mkdir(parents=True, mode=0o755)
        target = config / "config.env"
        body = f"KROGER_CLIENT_ID={self.client}\nKROGER_CLIENT_SECRET={self.secret}\nKROGER_LOCATION_ID={self.location}\n"
        target.write_text(body)
        os.chmod(target, 0o644)
        with self.assertRaises(grocy.GrocyError) as caught:
            grocy.load_kroger(self.home)
        self.assertNotIn(self.secret, str(caught.exception))
        os.chmod(target, 0o600)
        self.assertEqual(grocy.load_kroger(self.home), (self.client, self.secret, self.location, "FRYS"))
        target.unlink()
        with self.assertRaises(grocy.GrocyError) as missing:
            grocy.load_kroger(self.home)
        self.assertIn("not configured", str(missing.exception))

    def test_search_pages_with_a_total(self) -> None:
        KBOX.products = [
            kroger_product(f"{i:012d}", f"Avocado {i}", 1.25) for i in range(30)
        ]
        first = self.search("Avocado", start=0)
        self.assertEqual(len(first["hits"]), 24)
        self.assertEqual(first["start"], 0)
        self.assertEqual(first["total"], 30)
        self.assertEqual(first["hits"][0]["name"], "Avocado 0")
        self.assertTrue(any(
            call[1].startswith("/v1/products?") and "filter.limit=24" in call[1] and "filter.start=0" in call[1]
            for call in KBOX.calls
        ))
        second = self.search("Avocado", start=24)
        self.assertEqual([hit["name"] for hit in second["hits"]], [f"Avocado {i}" for i in range(24, 30)])
        self.assertEqual(second["total"], 30)
        empty = self.search("Avocado", start=48)
        self.assertEqual(empty["hits"], [])
        self.assertEqual(empty["total"], 30)
        with self.assertRaises(grocy.GrocyError):
            self.search("Avocado", start=241)
        self.assertEqual(BOX.calls, [])

    def plant_kroger(self, location: str | None = None) -> Path:
        config = Path(self.home) / ".config" / "jordan-grocy"
        config.mkdir(parents=True, mode=0o700)
        target = config / "config.env"
        target.write_text(
            "# store\n"
            "GROCY_URL=https://grocy.example\n"
            f"GROCY_API_KEY={KEY}\n"
            f"KROGER_CLIENT_ID={self.client}\n"
            f"KROGER_CLIENT_SECRET={self.secret}\n"
            f"KROGER_LOCATION_ID={location or self.location}\n"
        )
        os.chmod(target, 0o600)
        return target

    def test_nearby_stores_and_a_bad_zip(self) -> None:
        KBOX.locations = [
            {
                "locationId": "10000001",
                "name": "Example North",
                "chain": "FRYS",
                "address": {"addressLine1": "100 North St", "city": "Springfield", "zipCode": "10001"},
            },
            {
                "locationId": "10000002",
                "name": "Example South",
                "chain": "FRYS",
                "address": {"addressLine1": "200 South St", "city": "Springfield", "zipCode": "10002"},
            },
            {
                "locationId": "10000003",
                "name": "Other Market",
                "chain": "SMITHS",
                "address": {"addressLine1": "9 Main", "city": "Springfield", "zipCode": "10002"},
            },
        ]
        doc = grocy.nearby_stores(
            self.kroger.origin, self.client, self.secret, "10002", home=self.home, allow_loopback=True,
        )
        self.assertEqual([row["id"] for row in doc["stores"]], ["10000001", "10000002"])
        self.assertEqual(doc["stores"][0]["city"], "Springfield")
        self.assertTrue(any("filter.chain=FRYS" in call[1] and "filter.zipCode.near=10002" in call[1] for call in KBOX.calls))
        smiths = grocy.nearby_stores(
            self.kroger.origin, self.client, self.secret, "10002", home=self.home, allow_loopback=True, chain="SMITHS",
        )
        self.assertEqual([row["id"] for row in smiths["stores"]], ["10000003"])
        KBOX.calls = []
        with self.assertRaises(grocy.GrocyError):
            grocy.nearby_stores(
                self.kroger.origin, self.client, self.secret, "1000", home=self.home, allow_loopback=True,
            )
        self.assertEqual(KBOX.calls, [])

    def test_save_home_store_rewrites_only_the_location(self) -> None:
        target = self.plant_kroger()
        before = target.read_text()
        KBOX.locations = [{
            "locationId": "10000001",
            "name": "Example North",
            "chain": "FRYS",
            "address": {"addressLine1": "100 North St", "city": "Springfield", "zipCode": "10001"},
        }]
        saved = grocy.save_home_store(
            self.kroger.origin, self.client, self.secret, "10000001", home=self.home, allow_loopback=True,
        )
        self.assertEqual(saved["name"], "Example North")
        text = target.read_text()
        self.assertTrue(text.startswith("# store\n"))
        self.assertIn("GROCY_URL=https://grocy.example\n", text)
        self.assertEqual(text.count(f"GROCY_API_KEY={KEY}\n"), 1)
        self.assertEqual(text.count(f"KROGER_CLIENT_SECRET={self.secret}\n"), 1)
        self.assertIn(f"KROGER_CLIENT_ID={self.client}\n", text)
        self.assertIn("KROGER_LOCATION_ID=10000001\n", text)
        self.assertNotIn("KROGER_LOCATION_ID=10000002", text)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(grocy.load_kroger(self.home), (self.client, self.secret, "10000001", "FRYS"))
        self.assertNotEqual(text, before)
        KBOX.locations = []
        again = target.read_text()
        with self.assertRaises(grocy.GrocyError) as missing:
            grocy.save_home_store(
                self.kroger.origin, self.client, self.secret, "10000099", home=self.home, allow_loopback=True,
            )
        self.assertNotIn(self.secret, str(missing.exception))
        self.assertEqual(target.read_text(), again)
        KBOX.locations = [{
            "locationId": "10000003",
            "name": "Other Market",
            "chain": "SMITHS",
            "address": {"addressLine1": "9 Main", "city": "Springfield", "zipCode": "10002"},
        }]
        with self.assertRaises(grocy.GrocyError):
            grocy.save_home_store(
                self.kroger.origin, self.client, self.secret, "10000003", home=self.home, allow_loopback=True,
            )
        self.assertEqual(target.read_text(), again)
        self.assertEqual(grocy.load_kroger(self.home)[2], "10000001")

    def test_store_line_quantity_and_take_off(self) -> None:
        BOX.lists = [{"id": 7, "name": NAME}]
        BOX.lines = [
            {"id": 3, "product_id": 18, "shopping_list_id": 7, "amount": 2},
            {
                "id": 5,
                "product_id": None,
                "shopping_list_id": 7,
                "amount": 1,
                "note": "store|1.25|-|0001111050101|-|Avocado",
            },
        ]
        changed = grocy.set_store_amount(
            self.server.origin, KEY, 5, 3, home=self.home, allow_loopback=True, now=NOW,
        )
        self.assertEqual(changed["amount"], 3)
        self.assertEqual(next(row["amount"] for row in BOX.lines if row["id"] == 5), 3)
        with self.assertRaises(grocy.GrocyError):
            grocy.set_store_amount(self.server.origin, KEY, 3, 4, home=self.home, allow_loopback=True, now=NOW)
        grocy.remove_store_line(self.server.origin, KEY, 5, home=self.home, allow_loopback=True, now=NOW)
        self.assertEqual([row["id"] for row in BOX.lines], [3])
        with self.assertRaises(grocy.GrocyError):
            grocy.remove_store_line(self.server.origin, KEY, 3, home=self.home, allow_loopback=True, now=NOW)
        self.assertEqual([row["id"] for row in BOX.lines], [3])


def qml_command(script: Path) -> list[str]:
    # Ubuntu ships qmltime in the Qt prefix, so it can see the QML modules.
    # A bare `qml` on PATH is often a different runtime and exits 2 without
    # loading this file. qml6 is the Arch launcher the script was written for.
    override = os.environ.get("QML_RUNNER", "")
    if override:
        return [override, str(script)]
    for path in ("/usr/lib/qt6/bin/qmltime", "/usr/bin/qmltime"):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return [path, str(script)]
    found = shutil.which("qml6")
    if found:
        return [found, "-a", "core", "-f", str(script)]
    source = Path(__file__).resolve().parent / "qml_exit.cpp"
    runner_dir = Path("/tmp/jordan-grocy-qml-runner")
    runner_dir.mkdir(mode=0o755, exist_ok=True)
    binary = runner_dir / "qml-exit"
    (runner_dir / "qt.conf").write_text(
        "[Paths]\n"
        "Prefix=/usr\n"
        "Libraries=lib/x86_64-linux-gnu\n"
        "Plugins=lib/x86_64-linux-gnu/qt6/plugins\n"
        "Qml2Imports=lib/x86_64-linux-gnu/qt6/qml\n"
        "LibraryExecutables=lib/qt6/libexec\n"
        "Data=share/qt6\n",
        encoding="utf-8",
    )
    if not binary.exists() or binary.stat().st_mtime < source.stat().st_mtime:
        flags = subprocess.run(
            ["pkg-config", "--cflags", "--libs", "Qt6Gui", "Qt6Qml"],
            check=False,
            capture_output=True,
            text=True,
            env={key: value for key, value in os.environ.items() if key != "PKG_CONFIG_PATH"},
        )
        if flags.returncode != 0:
            raise AssertionError(flags.stderr or "Qt6 QML development files are not installed")
        built = subprocess.run(
            ["g++", "-std=c++17", "-fPIC", str(source), "-o", str(binary), *flags.stdout.split()],
            check=False,
            capture_output=True,
            text=True,
        )
        if built.returncode != 0:
            raise AssertionError(built.stderr or "could not build the QML test runner")
    return [str(binary), str(script)]


class ModelDecisionTest(unittest.TestCase):
    def test_search_exit_retries_only_an_in_flight_generation(self) -> None:
        script = Path(__file__).resolve().parent / "search_exit.qml"
        command = qml_command(script)
        env = os.environ.copy()
        env.pop("LD_LIBRARY_PATH", None)
        env.pop("LD_PRELOAD", None)
        env["QT_QPA_PLATFORM"] = "offscreen"
        runtime = Path("/tmp/jordan-grocy-runtime")
        runtime.mkdir(mode=0o700, exist_ok=True)
        env["XDG_RUNTIME_DIR"] = str(runtime)
        run = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        output = run.stdout + run.stderr
        detail = output or f"exit {run.returncode} from {command[0]}"
        self.assertIn("search-exit-ok", output, detail)
        self.assertNotIn("search-exit-bad", output, detail)
        # qmltime runs the file, then exits 255 because it never handles Qt.exit.
        if Path(command[0]).name != "qmltime":
            self.assertEqual(run.returncode, 0, detail)


if __name__ == "__main__":
    unittest.main()
