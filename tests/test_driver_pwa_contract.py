from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DRIVER_JS = (ROOT / "static" / "driver_app" / "driver.js").read_text(encoding="utf-8")
SERVICE_WORKER = (ROOT / "static" / "driver_app" / "sw.js").read_text(encoding="utf-8")


class DriverPwaContractTests(unittest.TestCase):
    def test_offline_database_keeps_snapshots_and_recovers_interrupted_sends(self):
        self.assertIn("DB_VERSION: 2", DRIVER_JS)
        self.assertIn("createObjectStore('snapshots'", DRIVER_JS)
        self.assertRegex(
            DRIVER_JS,
            r"records\.filter\(row => row\.status === 'syncing'\)[\s\S]*?record\.status = 'pending'",
        )

    def test_queue_is_scoped_and_duplicate_operations_are_blocked(self):
        self.assertIn("owner_driver_id", DRIVER_JS)
        self.assertIn("hasUnresolvedOperation(payload.route_id, payload.order_id)", DRIVER_JS)
        self.assertIn("this.syncInProgress", DRIVER_JS)
        self.assertIn("this.operationInProgress", DRIVER_JS)

    def test_queued_orders_are_visibly_locked(self):
        self.assertIn("Aguardando sincronização", DRIVER_JS)
        self.assertIn("!queued && !done && !problem", DRIVER_JS)
        self.assertIn("Finalizar esta parada", DRIVER_JS)

    def test_location_links_only_allow_http_and_have_maps_fallback(self):
        self.assertIn("['http:', 'https:'].includes(url.protocol)", DRIVER_JS)
        self.assertIn("mapsFallback", DRIVER_JS)
        self.assertIn("maps/search/?api=1", DRIVER_JS)

    def test_fetch_cache_write_is_awaited_inside_response_lifecycle(self):
        fetch_handler = SERVICE_WORKER.split("self.addEventListener('fetch'", 1)[1]
        self.assertIn("event.respondWith((async () =>", fetch_handler)
        self.assertIn("await cache.put(event.request, response.clone())", fetch_handler)
        self.assertNotIn("event.waitUntil", fetch_handler)


if __name__ == "__main__":
    unittest.main()
