from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from ripples import (
    VISITOR_COOKIE,
    Ripples,
    RipplesError,
    set_visitor_id,
    visitor_id_from_cookies,
)

VID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


class TestVisitorId:
    def setup_method(self):
        self.ripples = Ripples("priv_test")
        set_visitor_id(None)

    def teardown_method(self):
        set_visitor_id(None)

    def test_omitted_when_nothing_is_bound(self):
        """No visitor anywhere: the key is absent so the API assigns one."""
        self.ripples.signup("u1")
        assert "$visitor_id" not in self.ripples._queue[0]

    def test_ambient_visitor_id_is_attached_to_every_event_type(self):
        set_visitor_id(VID)
        self.ripples.signup("u1")
        self.ripples.identify("u1")
        self.ripples.track("did a thing", "u1")
        self.ripples.revenue(9.99, "u1")

        assert all(e["$visitor_id"] == VID for e in self.ripples._queue)

    def test_explicit_beats_ambient_beats_pinned(self):
        explicit = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        ambient = "11111111-2222-3333-4444-555555555555"

        r = Ripples("priv_test", visitor_id=VID)
        r.signup("u1")  # pinned only
        set_visitor_id(ambient)
        r.signup("u2")  # ambient wins over pinned
        r.signup("u3", visitor_id=explicit)  # explicit wins over both

        assert r._queue[0]["$visitor_id"] == VID
        assert r._queue[1]["$visitor_id"] == ambient
        assert r._queue[2]["$visitor_id"] == explicit

    def test_malformed_visitor_id_is_dropped_rather_than_forwarded(self):
        for bad in ("", "not-a-uuid", "../../etc/passwd", VID + "x", 12345):
            r = Ripples("priv_test")
            r.signup("u1", visitor_id=bad)
            assert "$visitor_id" not in r._queue[0], f"cookie: {bad!r}"

    def test_visitor_id_is_lowercased_and_trimmed(self):
        set_visitor_id(f"  {VID.upper()}  ")
        self.ripples.signup("u1")
        assert self.ripples._queue[0]["$visitor_id"] == VID

    def test_visitor_id_never_survives_as_a_custom_property(self):
        self.ripples.revenue(5.0, "u1", visitor_id=VID, plan="pro")
        event = self.ripples._queue[0]
        assert event["$visitor_id"] == VID
        assert "visitor_id" not in event
        assert event["plan"] == "pro"

    def test_ambient_id_does_not_leak_across_contexts(self):
        """A ContextVar, not a global: concurrent requests must not see each
        other's visitor."""
        import contextvars

        set_visitor_id(VID)

        def other_request() -> str | None:
            set_visitor_id("11111111-2222-3333-4444-555555555555")
            r = Ripples("priv_test")
            r.signup("them")
            return r._queue[0]["$visitor_id"]

        theirs = contextvars.copy_context().run(other_request)

        self.ripples.signup("me")
        assert theirs == "11111111-2222-3333-4444-555555555555"
        assert self.ripples._queue[0]["$visitor_id"] == VID


class TestVisitorIdFromCookies:
    def test_reads_the_tracker_cookie(self):
        assert visitor_id_from_cookies({VISITOR_COOKIE: VID}) == VID

    def test_absent_or_malformed_cookie_yields_none(self):
        assert visitor_id_from_cookies({}) is None
        assert visitor_id_from_cookies({VISITOR_COOKIE: ""}) is None
        assert visitor_id_from_cookies({VISITOR_COOKIE: "garbage"}) is None


class TestInit:
    def test_missing_key_raises(self):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RipplesError, match="Missing secret key"):
                Ripples()

    def test_key_from_env(self):
        with patch.dict("os.environ", {"RIPPLES_SECRET_KEY": "priv_test"}):
            r = Ripples()
            assert r._secret_key == "priv_test"

    def test_key_from_constructor(self):
        r = Ripples("priv_explicit")
        assert r._secret_key == "priv_explicit"

    def test_custom_base_url(self):
        r = Ripples("priv_test", base_url="https://custom.example.com/api/")
        assert r._base_url == "https://custom.example.com/api"

    def test_base_url_from_env(self):
        with patch.dict("os.environ", {"RIPPLES_URL": "https://env.example.com"}):
            r = Ripples("priv_test")
            assert r._base_url == "https://env.example.com"


class TestEnqueue:
    def setup_method(self):
        self.ripples = Ripples("priv_test")

    def test_revenue_enqueues(self):
        self.ripples.revenue(49.99, "user_1", currency="EUR")
        assert len(self.ripples._queue) == 1
        event = self.ripples._queue[0]
        assert event["$type"] == "revenue"
        assert event["$amount"] == 49.99
        assert event["$user_id"] == "user_1"
        assert event["currency"] == "EUR"
        assert "$sent_at" in event

    def test_signup_enqueues(self):
        self.ripples.signup("user_1", email="jane@example.com")
        event = self.ripples._queue[0]
        assert event["$type"] == "signup"
        assert event["$user_id"] == "user_1"
        assert event["email"] == "jane@example.com"

    def test_track_enqueues(self):
        self.ripples.track("created a budget", "user_1", area="budgets")
        event = self.ripples._queue[0]
        assert event["$type"] == "track"
        assert event["$name"] == "created a budget"
        assert event["$user_id"] == "user_1"
        assert event["$area"] == "budgets"

    def test_identify_enqueues(self):
        self.ripples.identify("user_1", email="jane@example.com", role="admin")
        event = self.ripples._queue[0]
        assert event["$type"] == "identify"
        assert event["$user_id"] == "user_1"
        assert event["email"] == "jane@example.com"
        assert event["role"] == "admin"

    def test_negative_revenue_for_refunds(self):
        self.ripples.revenue(-29.99, "user_1", transaction_id="txn_abc")
        event = self.ripples._queue[0]
        assert event["$amount"] == -29.99


class TestFlush:
    def setup_method(self):
        self.ripples = Ripples("priv_test")

    @patch.object(Ripples, "_post")
    def test_flush_sends_batch(self, mock_post):
        self.ripples.revenue(10.0, "user_1")
        self.ripples.signup("user_2")
        self.ripples.flush()

        mock_post.assert_called_once()
        path, data = mock_post.call_args[0]
        assert path == "/v1/ingest/batch"
        assert len(data["events"]) == 2
        assert self.ripples._queue == []

    @patch.object(Ripples, "_post")
    def test_flush_noop_when_empty(self, mock_post):
        self.ripples.flush()
        mock_post.assert_not_called()

    @patch.object(Ripples, "_post")
    def test_auto_flush_on_max_queue(self, mock_post):
        r = Ripples("priv_test", max_queue_size=3)
        r.revenue(1.0, "u1")
        r.revenue(2.0, "u2")
        mock_post.assert_not_called()
        r.revenue(3.0, "u3")  # triggers auto-flush
        mock_post.assert_called_once()


class TestErrorHandling:
    @patch.object(Ripples, "_post", side_effect=RipplesError("fail"))
    def test_errors_swallowed(self, mock_post):
        r = Ripples("priv_test")
        r.revenue(10.0, "user_1")
        r.flush()  # should not raise

    @patch.object(Ripples, "_post", side_effect=RipplesError("fail"))
    def test_on_error_callback(self, mock_post):
        callback = MagicMock()
        r = Ripples("priv_test", on_error=callback)
        r.revenue(10.0, "user_1")
        r.flush()
        callback.assert_called_once()
        assert isinstance(callback.call_args[0][0], RipplesError)


class TestPost:
    def setup_method(self):
        self.ripples = Ripples("priv_test")

    @patch("ripples.client.requests.Session.post")
    def test_post_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        self.ripples._post("/v1/ingest/batch", {"events": []})
        mock_post.assert_called_once()

    @patch("ripples.client.requests.Session.post")
    def test_post_raises_on_4xx(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 422
        mock_resp.content = b'{"error": "validation failed"}'
        mock_resp.json.return_value = {"error": "validation failed"}
        mock_post.return_value = mock_resp

        with pytest.raises(RipplesError, match="validation failed"):
            self.ripples._post("/v1/ingest/batch", {"events": []})


class TestTimestampOverride:
    """Backfilling historical events via the `timestamp=` parameter."""

    def setup_method(self):
        self.ripples = Ripples("priv_test")

    def test_omitted_timestamp_is_now_utc(self):
        self.ripples.signup("u1")
        event = self.ripples._queue[0]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", event["$sent_at"])
        # Parsed value should be within a few seconds of "now".
        parsed = datetime.strptime(event["$sent_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 5

    def test_aware_datetime_is_preserved(self):
        past = datetime(2024, 3, 15, 12, 0, 0, tzinfo=timezone.utc)
        self.ripples.track("did a thing", "u1", timestamp=past)
        assert self.ripples._queue[0]["$sent_at"] == "2024-03-15T12:00:00Z"

    def test_aware_datetime_in_non_utc_zone_is_converted(self):
        # 09:00 at UTC+2 → 07:00 UTC
        past = datetime(2024, 6, 1, 9, 0, 0, tzinfo=timezone(timedelta(hours=2)))
        self.ripples.revenue(29.99, "u1", timestamp=past)
        assert self.ripples._queue[0]["$sent_at"] == "2024-06-01T07:00:00Z"

    def test_naive_datetime_is_assumed_utc(self):
        past = datetime(2024, 1, 2, 3, 4, 5)  # no tzinfo
        self.ripples.signup("u1", timestamp=past)
        assert self.ripples._queue[0]["$sent_at"] == "2024-01-02T03:04:05Z"

    def test_iso_string_with_z_suffix(self):
        self.ripples.identify("u1", timestamp="2023-11-30T23:59:59Z")
        assert self.ripples._queue[0]["$sent_at"] == "2023-11-30T23:59:59Z"

    def test_iso_string_with_offset(self):
        self.ripples.identify("u1", timestamp="2023-11-30T23:59:59+02:00")
        assert self.ripples._queue[0]["$sent_at"] == "2023-11-30T21:59:59Z"

    def test_invalid_string_raises(self):
        with pytest.raises(RipplesError, match="Invalid timestamp"):
            self.ripples.track("did a thing", "u1", timestamp="not-a-date")

    def test_invalid_type_raises(self):
        with pytest.raises(RipplesError, match="timestamp must be"):
            self.ripples.track("did a thing", "u1", timestamp=1234567890)  # type: ignore[arg-type]

    def test_subscription_accepts_timestamp(self):
        past = datetime(2024, 2, 14, 15, 30, 0, tzinfo=timezone.utc)
        self.ripples.subscription("sub_1", "u1", "active", 29.0, timestamp=past)
        assert self.ripples._queue[0]["$sent_at"] == "2024-02-14T15:30:00Z"

    @patch.object(Ripples, "_post")
    def test_backfill_loop_across_auto_flush_boundary(self, mock_post):
        r = Ripples("priv_test", max_queue_size=10)
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        for i in range(25):
            r.track("did a thing", f"u{i}", timestamp=base + timedelta(days=i))
        r.flush()

        # 25 events → 10 + 10 + 5 across three batches.
        assert mock_post.call_count == 3
        all_events = []
        for call in mock_post.call_args_list:
            all_events.extend(call[0][1]["events"])
        assert len(all_events) == 25
        assert all_events[0]["$sent_at"] == "2024-01-01T00:00:00Z"
        assert all_events[24]["$sent_at"] == "2024-01-25T00:00:00Z"
