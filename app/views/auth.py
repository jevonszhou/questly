from flask import (Blueprint, flash, redirect, render_template, request,
                   session, url_for, current_app)
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..db import get_db
from ..models import (create_parent, get_user, has_any_parent,
                      verify_kid_pin, verify_parent)

bp = Blueprint("auth", __name__)


REMEMBER_COOKIE = "questly_email"


def _sign_in(user, remember=True):
    session.clear()
    # Permanent means a 30-day cookie; otherwise it dies with the browser.
    session.permanent = bool(remember)
    session["uid"] = str(user["_id"])
    session["role"] = user["role"]
    session["auth_version"] = int(user.get("auth_version", 0))


def _attempt_id(role, identity):
    material = f"{role}:{request.remote_addr or 'unknown'}:{identity.strip().lower()}"
    return hmac.new(current_app.config["SECRET_KEY"].encode(),
                    material.encode(), hashlib.sha256).hexdigest()


def _rate_limited(db, role, identity, limit=8):
    """Count failures in a 15-minute bucket without storing IP/email in clear."""
    bucket = int(datetime.now(timezone.utc).timestamp() // 900)
    key = f"{_attempt_id(role, identity)}:{bucket}"
    row = db.auth_attempts.find_one_and_update(
        {"_id": key},
        {"$inc": {"attempts": 1},
         "$setOnInsert": {"expires_at": datetime.now(timezone.utc) + timedelta(hours=1)}},
        upsert=True, return_document=True,
    )
    return row.get("attempts", 0) > limit


def _clear_attempts(db, role, identity):
    prefix = f"{_attempt_id(role, identity)}:"
    db.auth_attempts.delete_many({"_id": {"$regex": f"^{prefix}"}})


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """First run: create the first grown-up account."""
    db = get_db()
    if has_any_parent(db):
        return redirect(url_for("auth.parent_login"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        family_name = request.form.get("family_name", "我们的家").strip()[:80]
        timezone_name = request.form.get("timezone", "Asia/Shanghai").strip()

        error = None
        if not name or not email or not password:
            error = "请完整填写所有必填字段。"
        elif "@" not in email:
            error = "请输入有效的邮箱地址。"
        elif len(password) < 8:
            error = "密码至少需要 8 个字符。"
        elif password != confirm:
            error = "两次输入的密码不一致。"
        elif not family_name:
            error = "请填写家庭名称。"
        else:
            try:
                ZoneInfo(timezone_name)
            except (ZoneInfoNotFoundError, ValueError):
                error = "家庭时区无效。"

        if error:
            flash(error, "error")
        else:
            uid = create_parent(db, name, email, password)
            db.family_settings.update_one(
                {"_id": "family"},
                {"$set": {"family_name": family_name, "timezone": timezone_name,
                          "default_locale": "zh_CN", "updated_at": __import__("datetime").datetime.now(
                              __import__("datetime").timezone.utc), "updated_by": uid}},
                upsert=True)
            db.audit_logs.insert_one({
                "actor_id": uid, "actor_role": "parent", "action": "family.initialized",
                "target_type": "family_settings", "target_id": "family",
                "after": {"family_name": family_name, "timezone": timezone_name,
                          "default_locale": "zh_CN"},
                "reason": "首次初始化", "created_at": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc),
            })
            _sign_in(get_user(db, uid))
            flash("欢迎使用 Questly！请添加家庭成员。", "success")
            return redirect(url_for("parent.family"))

    return render_template("auth/setup.html")


@bp.route("/login", methods=["GET", "POST"])
def parent_login():
    db = get_db()
    if not has_any_parent(db):
        return redirect(url_for("auth.setup"))

    if request.method == "POST":
        email = request.form.get("email", "")
        remember = bool(request.form.get("remember"))
        if _rate_limited(db, "parent", email):
            flash("登录尝试次数过多，请稍后再试。", "error")
            return render_template("auth/parent_login.html",
                                   remembered=request.cookies.get(REMEMBER_COOKIE, "")), 429
        user = verify_parent(db, email, request.form.get("password", ""))
        if user:
            _clear_attempts(db, "parent", email)
            _sign_in(user, remember=remember)
            response = redirect(url_for("parent.dashboard"))
            # Remember the address to save typing next time — never the password.
            if remember:
                response.set_cookie(
                    REMEMBER_COOKIE, user["email"],
                    max_age=60 * 60 * 24 * 365, httponly=True, samesite="Lax",
                    secure=current_app.config["SESSION_COOKIE_SECURE"],
                )
            else:
                response.delete_cookie(REMEMBER_COOKIE)
            return response
        flash("邮箱或密码错误。", "error")

    return render_template(
        "auth/parent_login.html",
        remembered=request.cookies.get(REMEMBER_COOKIE, ""),
    )


@bp.route("/hi/<kid_id>", methods=["GET", "POST"])
def kid_login(kid_id):
    db = get_db()
    kid = get_user(db, kid_id)
    if not kid or kid["role"] != "kid" or kid.get("status") == "inactive":
        return redirect(url_for("public.who"))

    if request.method == "POST":
        if _rate_limited(db, "kid", str(kid["_id"])):
            flash("尝试次数过多，请稍后再试。", "error")
            return render_template("auth/kid_login.html", kid=kid), 429
        if verify_kid_pin(kid, request.form.get("pin", "")):
            _clear_attempts(db, "kid", str(kid["_id"]))
            _sign_in(kid, remember=True)
            return redirect(url_for("kid.home"))
        flash("PIN 不正确，请重试。", "error")
        return redirect(url_for("auth.kid_login", kid_id=kid_id))

    return render_template("auth/kid_login.html", kid=kid)


@bp.post("/logout")
def logout():
    session.clear()
    # The remembered email survives logout on purpose — it saves typing and
    # is not a credential. "Forget me" on the login page clears it.
    return redirect(url_for("public.who"))


@bp.post("/forget-me")
def forget_me():
    response = redirect(url_for("auth.parent_login"))
    response.delete_cookie(REMEMBER_COOKIE)
    flash("已清除已记住的邮箱，下次登录时需要重新输入。", "info")
    return response
