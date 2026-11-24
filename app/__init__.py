"""Questly — a family points-and-rewards app.

Copyright (C) 2026 Matt Hansford

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. See the LICENSE file, or <https://www.gnu.org/licenses/>.
"""

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, g, render_template, request, session
from markupsafe import Markup

try:  # local development convenience; in Docker the env comes from compose
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

from .db import get_db, init_mongo
from .models import COLORS, get_user, pending_count

APP_NAME = "Questly"


def create_app(overrides=None):
    app = Flask(__name__)
    app.config.from_mapping(
        APP_NAME=APP_NAME,
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        MONGO_URI=os.environ.get("MONGO_URI", "mongodb://localhost:27017/"),
        MONGO_DB=os.environ.get("MONGO_DB", "questly"),
        MONGO_TIMEOUT_MS=int(os.environ.get("MONGO_TIMEOUT_MS", "5000")),
        TIMEZONE=os.environ.get("TZ", "Asia/Shanghai"),
        PREFERRED_LOCALE=os.environ.get("PREFERRED_LOCALE", "zh_CN"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        MAX_CONTENT_LENGTH=1 * 1024 * 1024,
    )
    if overrides:
        app.config.update(overrides)
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("Set SECRET_KEY in the environment before starting Questly.")

    init_mongo(app)
    _register_security(app)
    _register_context(app)
    _register_filters(app)

    from .views import auth, kid, parent, public

    app.register_blueprint(public.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(kid.bp)
    app.register_blueprint(parent.bp)

    from . import cli
    cli.register(app)

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404,
                               message="找不到此页面。"), 404

    @app.errorhandler(500)
    def server_error(_e):
        return render_template("error.html", code=500,
                               message="服务器发生错误，请稍后重试。"), 500

    return app


# ---------------------------------------------------------------------------
# request lifecycle
# ---------------------------------------------------------------------------

def _register_security(app):
    import hmac
    import secrets

    @app.before_request
    def load_user():
        g.user = None
        uid = session.get("uid")
        if uid:
            user = get_user(get_db(), uid)
            if (user and user.get("role") == session.get("role")
                    and user.get("status") != "inactive"
                    and int(user.get("auth_version", 0)) == int(session.get("auth_version", 0))):
                g.user = user
            else:
                session.clear()

    @app.before_request
    def csrf_protect():
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return None
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
        expected = session.get("_csrf", "")
        if not expected or not hmac.compare_digest(str(sent), str(expected)):
            return render_template(
                "error.html", code=400,
                message="页面已过期，请返回上一页后重试。",
            ), 400
        return None

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    def csrf_token():
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_urlsafe(32)
        return session["_csrf"]

    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.globals["csrf_field"] = lambda: Markup(
        f'<input type="hidden" name="_csrf" value="{csrf_token()}">'
    )
    import uuid
    app.jinja_env.globals["idempotency_field"] = lambda: Markup(
        f'<input type="hidden" name="idempotency_key" value="{uuid.uuid4().hex}">'
    )


def _register_context(app):
    @app.before_request
    def apply_family_preferences():
        try:
            settings = get_db().family_settings.find_one({"_id": "family"}) or {}
            name = settings.get("timezone") or app.config["TIMEZONE"]
            app.config["TIMEZONE"] = name
            app.config["TZINFO"] = ZoneInfo(name)
            g.locale = (getattr(g, "user", None) or {}).get("locale") or \
                settings.get("default_locale") or app.config["PREFERRED_LOCALE"]
        except (ZoneInfoNotFoundError, ValueError):
            app.config["TZINFO"] = timezone.utc
            g.locale = app.config["PREFERRED_LOCALE"]

    @app.context_processor
    def inject():
        approvals = 0
        unread = 0
        user = getattr(g, "user", None)
        if user:
            try:
                from .notify import unread_count
                unread = unread_count(get_db(), user["_id"])
                if user["role"] == "parent":
                    approvals = pending_count(get_db())
            except Exception:
                pass
        return {
            "app_name": app.config["APP_NAME"],
            "locale": getattr(g, "locale", "zh_CN"),
            "celebrate": session.pop("celebrate", None),
            "current_user": getattr(g, "user", None),
            "approvals_waiting": approvals,
            "unread_news": unread,
            "palette": COLORS,
        }


def _register_filters(app):
    try:
        tz = ZoneInfo(app.config["TIMEZONE"])
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
    app.config["TZINFO"] = tz

    def as_local(value):
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(tz)

    @app.template_filter("datetime")
    def fmt_datetime(value):
        local = as_local(value)
        if not local:
            return ""
        if getattr(g, "locale", "zh_CN") == "zh_CN":
            return local.strftime("%Y年%m月%d日 %H:%M")
        return local.strftime("%-d %b, %-I:%M%p").replace("AM", "am").replace("PM", "pm")

    @app.template_filter("ago")
    def fmt_ago(value):
        local = as_local(value)
        if not local:
            return ""
        delta = datetime.now(tz) - local
        secs = int(delta.total_seconds())
        zh = getattr(g, "locale", "zh_CN") == "zh_CN"
        if secs < 60:
            return "刚刚" if zh else "just now"
        if secs < 3600:
            mins = secs // 60
            return f"{mins} 分钟前" if zh else f"{mins} min{'s' if mins > 1 else ''} ago"
        if secs < 86400:
            hrs = secs // 3600
            return f"{hrs} 小时前" if zh else f"{hrs} hour{'s' if hrs > 1 else ''} ago"
        days = secs // 86400
        if days < 7:
            return f"{days} 天前" if zh else f"{days} day{'s' if days > 1 else ''} ago"
        if zh:
            return local.strftime("%m月%d日")
        return local.strftime("%-d %b")

    @app.template_filter("signed")
    def fmt_signed(value):
        value = int(value or 0)
        return f"+{value}" if value > 0 else str(value)

    app.jinja_env.globals["local_now"] = lambda: datetime.now(tz)

    def greeting():
        hour = datetime.now(tz).hour
        if getattr(g, "locale", "zh_CN") == "zh_CN":
            if hour < 12:
                return "早上好"
            if hour < 17:
                return "下午好"
            return "晚上好"
        if hour < 12:
            return "Good morning"
        if hour < 17:
            return "Good afternoon"
        return "Good evening"

    app.jinja_env.globals["greeting"] = greeting

    from .notify import EVENTS
    app.jinja_env.globals["event_icon"] = (
        lambda e: EVENTS.get(e, {}).get("icon", "\U0001f514"))
