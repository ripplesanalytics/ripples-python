from __future__ import annotations

import atexit
import os
import re
from contextvars import ContextVar
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from typing import Any, Callable, Mapping

import requests

from .errors import RipplesError

SDK_NAME = "python"
try:
    SDK_VERSION = _pkg_version("ripples")
except PackageNotFoundError:
    SDK_VERSION = "0.0.0"

#: First-party cookie the browser tracker writes on every pageview.
VISITOR_COOKIE = "_rpl_vid"

_UUID_RE = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.IGNORECASE)

# A ContextVar rather than a module global: the client is normally a singleton
# shared by every worker thread and every concurrent coroutine, so a plain
# attribute would leak one request's visitor onto another request's events.
_ambient_visitor_id: ContextVar[str | None] = ContextVar(
    "ripples_visitor_id", default=None
)


def _normalize_visitor_id(value: Any) -> str | None:
    """Drop anything that isn't a well-formed UUID.

    Cookies are user-controlled and `visitor_id` is a UUID column at the other
    end, so a hand-edited value would fail the insert for the whole batch.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value.lower() if _UUID_RE.match(value) else None


def visitor_id_from_cookies(cookies: Mapping[str, str]) -> str | None:
    """Pull the tracker's visitor id out of a request's cookies.

    Works with anything dict-like: Django's ``request.COOKIES``, Flask's and
    FastAPI's ``request.cookies``. Returns None when the cookie is absent or
    malformed, which is the signal to let the API assign an id instead.
    """
    return _normalize_visitor_id(cookies.get(VISITOR_COOKIE))


def set_visitor_id(visitor_id: str | None) -> None:
    """Bind the browser visitor that events on this request belong to.

    Call it once where you already have the request, before any signup/track
    call in the same view or coroutine::

        ripples.set_visitor_id(ripples.visitor_id_from_cookies(request.COOKIES))

    Without it, a server-side signup lands on a synthetic per-user id and its
    acquisition channel is only recoverable once the browser identifies.
    """
    _ambient_visitor_id.set(_normalize_visitor_id(visitor_id))


def _format_timestamp(ts: datetime | str | None) -> str:
    if ts is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RipplesError(f"Invalid timestamp string: {ts!r}") from exc
    if not isinstance(ts, datetime):
        raise RipplesError(
            f"timestamp must be a datetime, ISO-8601 string, or None; got {type(ts).__name__}"
        )
    # Naive datetimes are assumed UTC — the SDK treats server-side code as UTC-native.
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ripples:
    """Official Python SDK for Ripples.sh — server-side event tracking.

    Events are queued in memory and sent as a single batch on flush().
    flush() is called automatically at interpreter exit via atexit.
    """

    def __init__(
        self,
        secret_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: int = 3,
        connect_timeout: int = 2,
        on_error: Callable[[Exception], None] | None = None,
        max_queue_size: int = 100,
        visitor_id: str | None = None,
    ) -> None:
        self._secret_key = secret_key or os.environ.get("RIPPLES_SECRET_KEY", "")
        if not self._secret_key:
            raise RipplesError(
                "Missing secret key. Set RIPPLES_SECRET_KEY in your environment "
                "or pass it to the constructor."
            )

        self._base_url = (
            (base_url or os.environ.get("RIPPLES_URL", "https://api.ripples.sh"))
            .rstrip("/")
        )
        self._timeout = (connect_timeout, timeout)
        self._on_error = on_error
        self._max_queue_size = max_queue_size
        # A static default for single-visitor processes; set_visitor_id() binds
        # per request and takes precedence over it.
        self._visitor_id = _normalize_visitor_id(visitor_id)
        self._queue: list[dict[str, Any]] = []

        self._session = requests.Session()
        self._session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self._secret_key}",
            }
        )

        atexit.register(self.flush)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def revenue(
        self,
        amount: float,
        user_id: str,
        *,
        timestamp: datetime | str | None = None,
        **attributes: Any,
    ) -> None:
        """Track revenue. Use negative amounts for refunds.

        Pass timestamp= (datetime or ISO-8601 string) to backfill a
        historical event; omit for "now".
        """
        self._enqueue(
            "revenue",
            {**attributes, "$amount": amount, "$user_id": user_id},
            timestamp=timestamp,
        )

    def signup(
        self,
        user_id: str,
        *,
        timestamp: datetime | str | None = None,
        **attributes: Any,
    ) -> None:
        """Track a signup.

        Bind the request's visitor first — via set_visitor_id() or a
        visitor_id= keyword — and the signup keeps the acquisition channel of
        the browsing session that produced it. Without one it still works, but
        the channel is only recoverable once the browser identifies.

        Pass timestamp= to backfill a historical event; omit for "now".
        """
        self._enqueue("signup", {**attributes, "$user_id": user_id}, timestamp=timestamp)

    def track(
        self,
        action_name: str,
        user_id: str,
        *,
        timestamp: datetime | str | None = None,
        **attributes: Any,
    ) -> None:
        """Track significant product usage only.

        Use for actions that prove a user got real value (created a budget,
        sent a message, invited a teammate). NOT a generic event log like
        PostHog or Mixpanel — do not send pageviews, banner impressions,
        button clicks, or "viewed X" events. Every track() call feeds the
        Activation dashboard; noise pollutes your funnel.

        Ripples auto-detects activation (first per user per action).
        Pass area= to group into product areas.
        Pass activated=True to flag this specific occurrence as the
        activation moment (not every occurrence of the event type).
        Pass timestamp= to backfill a historical event.
        """
        props = {k: v for k, v in attributes.items() if k not in ("area", "activated")}
        sys_fields: dict[str, Any] = {"$name": action_name, "$user_id": user_id}
        if "area" in attributes:
            sys_fields["$area"] = attributes["area"]
        if "activated" in attributes:
            sys_fields["$activated"] = attributes["activated"]
        self._enqueue("track", {**props, **sys_fields}, timestamp=timestamp)

    def subscription(
        self,
        subscription_id: str,
        user_id: str,
        status: str,
        amount: float,
        interval: str = "month",
        *,
        timestamp: datetime | str | None = None,
        **attributes: Any,
    ) -> None:
        """Track a subscription state change for MRR calculation.

        Call when a subscription is created, upgraded/downgraded, or canceled.
        For Stripe/Paddle users with a native integration, MRR is tracked
        automatically — only use this for other payment providers.

        Args:
            subscription_id: Your subscription ID (must be stable across updates).
            user_id: The user who owns the subscription.
            status: active, canceled, past_due, trialing, or paused.
            amount: Amount per billing cycle (e.g. 29.00), in your currency.
            interval: Billing interval: month, year, week, or day.
            timestamp: Override event time for backfilling history.
            **attributes: Optional: currency, name/plan, interval_count.
        """
        name = attributes.pop("name", attributes.pop("plan", None))
        currency = attributes.pop("currency", None)
        interval_count = attributes.pop("interval_count", 1)

        # User properties first, then system fields on top (can't be overwritten).
        event: dict[str, Any] = {
            **attributes,
            "$amount": 0,
            "$user_id": user_id,
            "subscription_id": subscription_id,
            "subscription_status": status,
            "subscription_amount": str(round(amount * 100)),
            "billing_interval": interval,
            "billing_interval_count": str(interval_count),
        }
        if currency is not None:
            event["currency"] = currency
        if name is not None:
            event["$name"] = name
        self._enqueue("revenue", event, timestamp=timestamp)

    def identify(
        self,
        user_id: str,
        *,
        timestamp: datetime | str | None = None,
        **attributes: Any,
    ) -> None:
        """Identify a user (set or update traits).

        Pass timestamp= to backdate the identify event; omit for "now".
        """
        self._enqueue("identify", {**attributes, "$user_id": user_id}, timestamp=timestamp)

    def flush(self) -> None:
        """Send all queued events in a single batch request.

        Called automatically at interpreter exit. Call explicitly when you
        need to guarantee delivery before a process ends.
        """
        if not self._queue:
            return

        batch, self._queue = self._queue, []
        self._send("/v1/ingest/batch", {"events": batch})

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _enqueue(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        timestamp: datetime | str | None = None,
    ) -> None:
        # An explicit visitor_id= on the call beats the one bound to this
        # request, which beats the client's pinned default. Pop both spellings
        # unconditionally so neither survives as a custom property.
        unprefixed = data.pop("visitor_id", None)
        prefixed = data.pop("$visitor_id", None)
        visitor_id = _normalize_visitor_id(
            prefixed or unprefixed or _ambient_visitor_id.get() or self._visitor_id
        )

        event = {
            **data,
            "$type": event_type,
            "$sent_at": _format_timestamp(timestamp),
            "$sdk_name": SDK_NAME,
            "$sdk_version": SDK_VERSION,
            "$platform": "server",
        }

        # Omit the key entirely when there is no visitor — the API then mints a
        # stable per-user id of its own, exactly as it did before this existed.
        # Sending "" instead would be read as a real id and break that fallback.
        if visitor_id is not None:
            event["$visitor_id"] = visitor_id

        self._queue.append(event)
        if len(self._queue) >= self._max_queue_size:
            self.flush()

    def _send(self, path: str, data: dict[str, Any]) -> None:
        """Dispatch a request, swallowing errors so the host app is never
        disrupted by a Ripples outage."""
        try:
            self._post(path, data)
        except Exception as exc:
            if self._on_error is not None:
                self._on_error(exc)

    def _post(self, path: str, data: dict[str, Any]) -> None:
        """Send a POST request. Override in a subclass to swap HTTP clients."""
        url = f"{self._base_url}{path}"
        resp = self._session.post(url, json=data, timeout=self._timeout)

        if resp.status_code >= 400:
            body = resp.json() if resp.content else {}
            message = body.get("error", f"HTTP {resp.status_code}")
            raise RipplesError(message, status_code=resp.status_code)
