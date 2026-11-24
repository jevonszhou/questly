from flask import (Blueprint, flash, g, jsonify, redirect, render_template,
                   request, url_for, current_app)

from ..db import get_db
from ..models import (AVATARS, COLORS, PARENT_AVATARS, REPEAT_CHOICES,
                      STOCK_PERIODS, STOCK_SCOPES, adjust_points, check_password,
                      parse_subtasks,
                      create_kid, create_parent, decide_quest_claim,
                      decide_redemption, get_quest, get_reward, get_user,
                      history_for, list_kids, list_parents, list_quests,
                      list_rewards, oid, pending_claims, pending_redemptions,
                      recent_activity, redemptions_for, set_kid_pin,
                      add_channel, get_channel, remove_channel,
                      update_channel,
                      set_parent_password, stock_label, stock_mode,
                      update_parent)
from ..notify import (CHANNELS, channel_summary, compose, deliver,
                      signal_groups,
                      events_for, fanout, mark_all_read, notify, notify_many,
                      recent)
from .helpers import as_int, check_goal_reached, parent_required, safe_next
from ..services.pets import (DEFAULT_PET_RULES, active_pet, adjust_pet_hp,
                             adopt_pet, pet_history, status_for,
                             update_pet_rules)

bp = Blueprint("parent", __name__, url_prefix="/parent")

QUICK_AWARDS = [10, 25, 50, 100]


@bp.get("/")
@parent_required
def dashboard():
    db = get_db()
    kids = list_kids(db)
    return render_template(
        "parent/dashboard.html",
        kids=kids,
        quick=QUICK_AWARDS,
        activity=recent_activity(db, limit=12),
        redemptions=pending_redemptions(db),
        claims=pending_claims(db),
    )


# ---------------------------------------------------------------------------
# points
# ---------------------------------------------------------------------------

@bp.post("/award")
@parent_required
def award():
    db = get_db()
    kid_id = request.form.get("kid_id", "")
    try:
        amount = abs(int(request.form.get("amount", "0").strip()))
    except (TypeError, ValueError):
        amount = 0
    reason = request.form.get("reason", "").strip()[:240]

    # The "Take away" button reuses the same amount box.
    if request.form.get("negate"):
        amount = -abs(amount)

    if amount < 1 or amount > 1000:
        flash("金额必须为 1 至 1000 的整数。", "warn")
        return redirect(safe_next(url_for("parent.dashboard")))
    if not reason:
        flash("请填写积分变动原因。", "warn")
        return redirect(safe_next(url_for("parent.dashboard")))

    kind = "parent_award" if amount > 0 else "parent_deduct"
    kid, entry = adjust_points(
        db, kid_id, amount, reason, g.user, kind=kind,
        idempotency_key=request.form.get("idempotency_key"),
    )
    if not kid:
        target = get_user(db, kid_id)
        if amount < 0 and target and target.get("role") == "kid":
            flash(f"积分不足。当前余额为 {target.get('points', 0)}，不能扣除 {abs(amount)}。", "error")
        else:
            flash("找不到这个孩子。", "error")
    else:
        if amount > 0:
            fanout(db, kid, "points_awarded", f"⭐ 获得 {amount} 积分",
                   compose(reason, f"来自：{g.user['name']}",
                           f"当前余额：{kid.get('points', 0)}"))
            check_goal_reached(db, kid["_id"])
        verb = "奖励" if amount > 0 else "扣除"
        flash(f"已为 {kid['name']}{verb} {abs(amount)} 积分。", "success")
    return redirect(safe_next(url_for("parent.dashboard")))


@bp.get("/kid/<kid_id>")
@parent_required
def kid_detail(kid_id):
    db = get_db()
    kid = get_user(db, kid_id)
    if not kid or kid["role"] != "kid":
        flash("Couldn't find that child.", "error")
        return redirect(url_for("parent.dashboard"))
    return render_template(
        "parent/kid_detail.html",
        kid=kid,
        quick=QUICK_AWARDS,
        history=history_for(db, kid["_id"], limit=60),
        redemptions=redemptions_for(db, kid["_id"], limit=20),
        avatars=AVATARS,
        pet=active_pet(db, kid["_id"]),
    )


@bp.route("/kid/<kid_id>/pet", methods=["GET", "POST"])
@parent_required
def kid_pet(kid_id):
    db = get_db()
    kid = get_user(db, kid_id)
    if not kid or kid.get("role") != "kid":
        flash("找不到这个孩子。", "error")
        return redirect(url_for("parent.family"))
    if request.method == "POST":
        action = request.form.get("action")
        if action == "adjust":
            try:
                delta = int(request.form.get("delta", "0"))
            except (TypeError, ValueError):
                delta = 0
            event, error = adjust_pet_hp(db, kid, delta,
                                         request.form.get("reason", ""), g.user)
            flash(error or f"宠物生命值已调整为 {event['hp_after']}。",
                  "error" if error else "success")
        elif action == "adopt":
            pet, error = adopt_pet(db, kid, request.form.get("name", ""),
                                   request.form.get("species", "cat"), g.user)
            flash(error or f"已为 {kid['name']} 设置新的宠物。",
                  "error" if error else "success")
        return redirect(url_for("parent.kid_pet", kid_id=kid_id))
    companion = active_pet(db, kid["_id"])
    if companion:
        companion["status"] = status_for(
            companion.get("hp", 0),
            (db.family_settings.find_one({"_id": "family"}) or {})
            .get("pet_rules", DEFAULT_PET_RULES))
    history = list(db.pet_events.find({"kid_id": kid["_id"]})
                   .sort("created_at", -1).limit(100))
    pets = list(db.pets.find({"kid_id": kid["_id"]}).sort("created_at", -1))
    return render_template("parent/pet.html", kid=kid, pet=companion,
                           history=history, pets=pets)


@bp.route("/pet-rules", methods=["GET", "POST"])
@parent_required
def pet_rules():
    db = get_db()
    settings = db.family_settings.find_one({"_id": "family"}) or {}
    rules = dict(DEFAULT_PET_RULES)
    rules.update(settings.get("pet_rules", {}))
    if request.method == "POST":
        try:
            maximum = int(request.form.get("max_hp", "100"))
            numerator = int(request.form.get("ratio_numerator", "1"))
            denominator = int(request.form.get("ratio_denominator", "1"))
        except (TypeError, ValueError):
            maximum, numerator, denominator = 0, 0, 0
        if maximum > 0:
            import math
            care = max(1, math.ceil(maximum * .2))
            good = max(care, math.ceil(maximum * .5))
            healthy = max(good, math.ceil(maximum * .8))
            ranges = [(1, care - 1, "weak", "虚弱"),
                      (care, good - 1, "care", "需要照顾"),
                      (good, healthy - 1, "good", "良好"),
                      (healthy, maximum, "healthy", "健康")]
            thresholds = [{"min": 0, "max": 0, "status": "recover", "label_zh": "需要恢复"}]
            thresholds.extend({"min": low, "max": high, "status": state, "label_zh": label}
                              for low, high, state, label in ranges if low <= high)
        else:
            thresholds = []
        updated_rules = {
            "enabled": bool(request.form.get("enabled")),
            "max_hp": maximum, "ratio_numerator": numerator,
            "ratio_denominator": denominator,
            "apply_kinds": [kind for kind in request.form.getlist("apply_kinds")
                            if kind in {"quest", "parent_award", "parent_deduct"}],
            "thresholds": thresholds,
        }
        ok, error = update_pet_rules(db, g.user, updated_rules,
                                     request.form.get("reason", ""))
        flash(error or "宠物规则已保存。", "error" if error else "success")
        return redirect(url_for("parent.pet_rules"))
    return render_template("parent/pet_rules.html", rules=rules)


@bp.route("/settings", methods=["GET", "POST"])
@parent_required
def settings():
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    db = get_db()
    doc = db.family_settings.find_one({"_id": "family"}) or {}
    if request.method == "POST":
        name = request.form.get("family_name", "").strip()[:80]
        locale = request.form.get("default_locale", "zh_CN")
        timezone_name = request.form.get("timezone", "Asia/Shanghai")
        error = None
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            error = "时区无效。"
        if locale != "zh_CN":
            error = "当前版本仅支持简体中文。"
        if not name:
            error = "请填写家庭名称。"
        if error:
            flash(error, "error")
        else:
            before = {k: doc.get(k) for k in ("family_name", "default_locale", "timezone")}
            after = {"family_name": name, "default_locale": locale,
                     "timezone": timezone_name}
            db.family_settings.update_one(
                {"_id": "family"},
                {"$set": {**after, "updated_at": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc), "updated_by": g.user["_id"]}},
                upsert=True)
            db.audit_logs.insert_one({
                "actor_id": g.user["_id"], "actor_role": "parent",
                "action": "family_settings.updated", "target_type": "family_settings",
                "target_id": "family", "before": before, "after": after,
                "reason": "家长更新家庭设置", "created_at": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc),
            })
            current_app.config["TIMEZONE"] = timezone_name
            current_app.config["TZINFO"] = ZoneInfo(timezone_name)
            flash("家庭设置已保存。", "success")
        return redirect(url_for("parent.settings"))
    doc.setdefault("family_name", "我们的家")
    doc.setdefault("default_locale", "zh_CN")
    doc.setdefault("timezone", "Asia/Shanghai")
    return render_template("parent/settings.html", settings=doc)


# ---------------------------------------------------------------------------
# approvals
# ---------------------------------------------------------------------------

@bp.get("/approvals")
@parent_required
def approvals():
    db = get_db()
    return render_template(
        "parent/approvals.html",
        redemptions=pending_redemptions(db),
        claims=pending_claims(db),
    )


@bp.post("/approvals/redemption/<redemption_id>")
@parent_required
def decide_redemption_route(redemption_id):
    approve = request.form.get("decision") == "approve"
    red = decide_redemption(get_db(), redemption_id, approve, g.user)
    if not red:
        flash("此申请已处理。", "warn")
    elif approve:
        fanout(get_db(), red["kid_id"], "purchase_approved",
               f"{red['reward_emoji']} {red['reward_title']} 已交付",
               f"{g.user['name']} 已确认交付。兑换花费 {red['cost']} 积分。")
        flash(f"已将「{red['reward_title']}」交付给 {red['kid_name']}。", "success")
    else:
        db = get_db()
        fanout(db, red["kid_id"], "purchase_rejected",
               f"{red['reward_title']} 未获批准", f"{red['cost']} 积分已退回。")
        check_goal_reached(db, red["kid_id"])
        flash(f"已拒绝「{red['reward_title']}」，退回 {red['cost']} 积分。", "info")
    return redirect(safe_next(url_for("parent.approvals")))


@bp.post("/approvals/quest/<claim_id>")
@parent_required
def decide_claim_route(claim_id):
    approve = request.form.get("decision") == "approve"
    claim = decide_quest_claim(get_db(), claim_id, approve, g.user)
    if not claim:
        flash("此申请已处理。", "warn")
    elif approve:
        db = get_db()
        fanout(db, claim["kid_id"], "quest_approved",
               f"{claim['quest_emoji']} {claim['quest_title']} 已通过",
               f"获得 {claim['points']} 积分。审批人：{g.user['name']}。")
        check_goal_reached(db, claim["kid_id"])
        flash(f"{claim['kid_name']} 完成「{claim['quest_title']}」，获得 {claim['points']} 积分。", "success")
    else:
        fanout(get_db(), claim["kid_id"], "quest_rejected",
               f"{claim['quest_title']} 未通过", "请根据家长提示再次尝试。")
        flash(f"已将「{claim['quest_title']}」退回给 {claim['kid_name']}。", "info")
    return redirect(safe_next(url_for("parent.approvals")))


# ---------------------------------------------------------------------------
# rewards
# ---------------------------------------------------------------------------

def _stock_fields(form, existing=None):
    """Read the stock controls off the reward form into storable fields."""
    mode = form.get("stock_mode")
    if mode not in ("unlimited", "fixed", "periodic"):
        # No explicit mode: infer from whether a plain stock number was sent,
        # so a form without the newer controls still behaves as it used to.
        mode = "fixed" if form.get("stock", "").strip() else "unlimited"

    fields = {"stock_mode": mode, "stock": None,
              "stock_limit": None, "stock_period": None, "stock_scope": None}

    if mode == "fixed":
        default = (existing or {}).get("stock") or 0
        fields["stock"] = as_int(form.get("stock"), default=default, low=0, high=9999)
    elif mode == "periodic":
        fields["stock_limit"] = as_int(form.get("stock_limit"), default=1, low=1, high=999)
        period = form.get("stock_period", "daily")
        fields["stock_period"] = period if period in STOCK_PERIODS else "daily"
        scope = form.get("stock_scope", "child")
        fields["stock_scope"] = scope if scope in STOCK_SCOPES else "child"
    return fields


@bp.get("/rewards")
@parent_required
def rewards():
    db = get_db()
    rewards = [dict(r, stock_text=stock_label(r), mode=stock_mode(r))
               for r in list_rewards(db, active_only=False)]
    return render_template("parent/rewards.html", rewards=rewards,
                           periods=STOCK_PERIODS, scopes=STOCK_SCOPES)


@bp.post("/rewards")
@parent_required
def create_reward():
    db = get_db()
    title = request.form.get("title", "").strip()[:80]
    if not title:
        flash("请填写奖励名称。", "error")
        return redirect(url_for("parent.rewards"))

    doc = {
        "title": title,
        "description": request.form.get("description", "").strip()[:240],
        "emoji": (request.form.get("emoji", "").strip() or "\U0001f381")[:4],
        "cost": as_int(request.form.get("cost"), default=10, low=1, high=100000),
        "active": True,
    }
    doc.update(_stock_fields(request.form))
    db.rewards.insert_one(doc)
    notify_many(db, list_kids(db), "shop_new",
                f"{doc['emoji']} New in the shop: {title}",
                compose(f"Costs {doc['cost']} points",
                        doc.get("description"),
                        stock_label(doc) and f"Available: {stock_label(doc)}"))
    flash(f"已将「{title}」添加到奖励商城。", "success")
    return redirect(url_for("parent.rewards"))


@bp.post("/rewards/<reward_id>")
@parent_required
def update_reward(reward_id):
    db = get_db()
    reward = get_reward(db, reward_id)
    if not reward:
        flash("Couldn't find that reward.", "error")
        return redirect(url_for("parent.rewards"))

    action = request.form.get("action")
    if action == "toggle":
        db.rewards.update_one({"_id": reward["_id"]},
                              {"$set": {"active": not reward.get("active", True)}})
        flash(f"'{reward['title']}' is now "
              f"{'hidden from' if reward.get('active') else 'back in'} the shop.", "info")
    elif action == "delete":
        db.rewards.delete_one({"_id": reward["_id"]})
        # Don't leave anyone saving up for something that no longer exists.
        db.users.update_many({"goal_reward_id": reward["_id"]},
                             {"$set": {"goal_reward_id": None}})
        flash(f"Removed '{reward['title']}'.", "info")
    else:
        changes = {
            "title": request.form.get("title", reward["title"]).strip()[:80] or reward["title"],
            "description": request.form.get("description", "").strip()[:240],
            "emoji": (request.form.get("emoji", "").strip() or "\U0001f381")[:4],
            "cost": as_int(request.form.get("cost"), default=reward["cost"], low=1, high=100000),
        }
        changes.update(_stock_fields(request.form, reward))
        db.rewards.update_one({"_id": reward["_id"]}, {"$set": changes})
        flash(f"Updated '{reward['title']}'.", "success")
    return redirect(url_for("parent.rewards"))


# ---------------------------------------------------------------------------
# quests
# ---------------------------------------------------------------------------

@bp.get("/quests")
@parent_required
def quests():
    db = get_db()
    return render_template("parent/quests.html",
                           quests=list_quests(db, active_only=False),
                           kids=list_kids(db),
                           repeats=REPEAT_CHOICES)


@bp.post("/quests")
@parent_required
def create_quest():
    db = get_db()
    title = request.form.get("title", "").strip()[:80]
    if not title:
        flash("请填写任务名称。", "error")
        return redirect(url_for("parent.quests"))

    repeat = request.form.get("repeat", "daily")
    assigned = [oid(v) for v in request.form.getlist("assigned_to")]
    db.quests.insert_one({
        "title": title,
        "emoji": (request.form.get("emoji", "").strip() or "⭐")[:4],
        "points": as_int(request.form.get("points"), default=5, low=1, high=1000),
        "repeat": repeat if repeat in REPEAT_CHOICES else "daily",
        "times_per_period": as_int(request.form.get("times_per_period"),
                                   default=1, low=1, high=20),
        "description": request.form.get("description", "").strip()[:240],
        "subtasks": parse_subtasks(request.form.get("subtasks", "")),
        "assigned_to": [a for a in assigned if a],
        "active": True,
    })
    flash(f"已添加任务「{title}」。", "success")
    return redirect(url_for("parent.quests"))


@bp.post("/quests/<quest_id>")
@parent_required
def update_quest(quest_id):
    db = get_db()
    quest = get_quest(db, quest_id)
    if not quest:
        flash("Couldn't find that quest.", "error")
        return redirect(url_for("parent.quests"))

    action = request.form.get("action")
    if action == "toggle":
        db.quests.update_one({"_id": quest["_id"]},
                             {"$set": {"active": not quest.get("active", True)}})
        flash(f"'{quest['title']}' is now "
              f"{'paused' if quest.get('active') else 'active'}.", "info")
    elif action == "delete":
        db.quests.delete_one({"_id": quest["_id"]})
        db.quest_claims.delete_many({"quest_id": quest["_id"], "status": "pending"})
        flash(f"Removed '{quest['title']}'.", "info")
    else:
        repeat = request.form.get("repeat", quest.get("repeat", "daily"))
        assigned = [oid(v) for v in request.form.getlist("assigned_to")]
        db.quests.update_one({"_id": quest["_id"]}, {"$set": {
            "title": request.form.get("title", quest["title"]).strip()[:80] or quest["title"],
            "emoji": (request.form.get("emoji", "").strip() or "⭐")[:4],
            "points": as_int(request.form.get("points"), default=quest["points"], low=1, high=1000),
            "repeat": repeat if repeat in REPEAT_CHOICES else "daily",
            "times_per_period": as_int(request.form.get("times_per_period"),
                                       default=quest.get("times_per_period", 1),
                                       low=1, high=20),
            "description": request.form.get("description", "").strip()[:240],
            "subtasks": parse_subtasks(request.form.get("subtasks", ""),
                                       quest.get("subtasks")),
            "assigned_to": [a for a in assigned if a],
        }})
        flash(f"Updated '{quest['title']}'.", "success")
    return redirect(url_for("parent.quests"))


# ---------------------------------------------------------------------------
# family
# ---------------------------------------------------------------------------

@bp.get("/family")
@parent_required
def family():
    db = get_db()
    return render_template("parent/family.html",
                           kids=list_kids(db),
                           parents=list_parents(db),
                           avatars=AVATARS,
                           colors=COLORS,
                           channels=CHANNELS,
                           kid_events=events_for("kid"),
                           summary=channel_summary)


@bp.post("/family/kids")
@parent_required
def add_kid():
    db = get_db()
    name = request.form.get("name", "").strip()[:40]
    if not name:
        flash("请填写孩子姓名。", "error")
        return redirect(url_for("parent.family"))

    pin = request.form.get("pin", "").strip()
    if not (pin.isdigit() and 4 <= len(pin) <= 6):
        flash("请设置 4 至 6 位数字 PIN。", "error")
        return redirect(url_for("parent.family"))

    create_kid(db, name,
               request.form.get("avatar") or AVATARS[0],
               request.form.get("color") or COLORS[0][1],
               pin=pin or None,
               points=as_int(request.form.get("points"), default=0, low=0, high=100000),
               actor=g.user)
    db.audit_logs.insert_one({
        "actor_id": g.user["_id"], "actor_role": "parent", "action": "kid.created",
        "target_type": "user", "target_id": name,
        "reason": "添加家庭成员", "created_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc),
    })
    flash(f"已添加 {name}，可从首页登录。", "success")
    return redirect(url_for("parent.family"))


@bp.post("/family/kids/<kid_id>")
@parent_required
def update_kid(kid_id):
    db = get_db()
    kid = get_user(db, kid_id)
    if not kid or kid["role"] != "kid":
        flash("Couldn't find that child.", "error")
        return redirect(url_for("parent.family"))

    action = request.form.get("action")
    if action == "delete":
        db.users.update_one({"_id": kid["_id"]}, {"$set": {"status": "inactive"}})
        db.audit_logs.insert_one({
            "actor_id": g.user["_id"], "actor_role": "parent",
            "action": "kid.deactivated", "target_type": "user",
            "target_id": str(kid["_id"]), "reason": "家长停用儿童账户",
            "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        })
        flash(f"已停用 {kid['name']} 的账户，积分及历史记录已保留。", "info")
        return redirect(url_for("parent.family"))

    if action == "pin":
        pin = request.form.get("pin", "").strip()
        if not (pin.isdigit() and 4 <= len(pin) <= 6):
            flash("PIN 必须为 4 至 6 位数字。", "error")
        else:
            set_kid_pin(db, kid["_id"], pin or None)
            db.audit_logs.insert_one({
                "actor_id": g.user["_id"], "actor_role": "parent",
                "action": "kid.pin.updated", "target_type": "user",
                "target_id": str(kid["_id"]), "reason": "家长重置儿童 PIN",
                "created_at": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc),
            })
            flash(f"已更新 {kid['name']} 的 PIN。", "success")
        return redirect(url_for("parent.family"))

    before = {"name": kid.get("name"), "avatar": kid.get("avatar"),
              "color": kid.get("color")}
    after = {
        "name": request.form.get("name", kid["name"]).strip()[:40] or kid["name"],
        "avatar": request.form.get("avatar") or kid.get("avatar"),
        "color": request.form.get("color") or kid.get("color"),
    }
    db.users.update_one({"_id": kid["_id"]}, {"$set": after})
    db.audit_logs.insert_one({
        "actor_id": g.user["_id"], "actor_role": "parent", "action": "kid.updated",
        "target_type": "user", "target_id": str(kid["_id"]),
        "before": before, "after": after, "reason": "更新儿童资料",
        "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    })
    flash("资料已保存。", "success")
    return redirect(url_for("parent.family"))


@bp.post("/family/parents")
@parent_required
def add_parent():
    db = get_db()
    name = request.form.get("name", "").strip()[:40]
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if not name or "@" not in email:
        flash("Need a name and a valid email address.", "error")
    elif len(password) < 8:
        flash("Use a password of at least 8 characters.", "error")
    elif db.users.find_one({"email": email}):
        flash("Someone already uses that email.", "error")
    else:
        create_parent(db, name, email, password)
        flash(f"{name} can now log in as a grown-up.", "success")
    return redirect(url_for("parent.family"))


@bp.get("/news")
@parent_required
def news():
    db = get_db()
    items = recent(db, g.user["_id"], limit=50)
    mark_all_read(db, g.user["_id"])
    return render_template("parent/news.html", items=items)


# ---------------------------------------------------------------------------
# notification channels
# ---------------------------------------------------------------------------

def _channel_owner(db, owner_id):
    """A grown-up may manage their own channels and any child's, but not
    another grown-up's — those are personal."""
    if str(owner_id) == str(g.user["_id"]):
        return g.user
    target = get_user(db, owner_id)
    if target and target["role"] == "kid":
        return target
    return None


def _owner_redirect(owner):
    if owner["role"] == "kid":
        return redirect(url_for("parent.family") + f"#kid-{owner['_id']}")
    return redirect(url_for("parent.account"))


@bp.post("/signal/groups")
@parent_required
def lookup_signal_groups():
    """Ask a Signal bridge which groups a number belongs to, so the group id
    can be picked from a list rather than copied by hand."""
    groups, error = signal_groups(request.form.get("api_url", "").strip(),
                                  request.form.get("number", "").strip())
    return jsonify(groups=groups, error=error)


@bp.post("/channels/<owner_id>")
@parent_required
def add_channel_route(owner_id):
    db = get_db()
    owner = _channel_owner(db, owner_id)
    if not owner:
        flash("You can only set up your own channels, or a child's.", "error")
        return redirect(url_for("parent.account"))

    kind = request.form.get("type", "")
    spec = CHANNELS.get(kind)
    if not spec:
        flash("Pick a notification type.", "error")
        return _owner_redirect(owner)

    config, missing = {}, []
    for key, label, _t, required, _ph, _help in spec["fields"]:
        value = request.form.get(f"{kind}__{key}", "").strip()
        if value:
            config[key] = value
        elif required:
            missing.append(label)
    if missing:
        flash(f"{spec['label']} needs: {', '.join(missing)}.", "error")
        return _owner_redirect(owner)

    allowed = set(events_for(owner["role"]))
    events = [e for e in request.form.getlist("events") if e in allowed]

    channel = add_channel(db, owner["_id"], kind, config, events)
    ok, detail = deliver(channel, "Questly is connected",
                         f"Notifications for {owner['name']} will arrive here.")
    if ok:
        flash(f"{spec['label']} connected — a test message is on its way.", "success")
    else:
        flash(f"Saved, but the test message failed: {detail}", "warn")
    return _owner_redirect(owner)


@bp.post("/channels/<owner_id>/<channel_id>/edit")
@parent_required
def edit_channel(owner_id, channel_id):
    db = get_db()
    owner = _channel_owner(db, owner_id)
    channel = get_channel(db, owner_id, channel_id) if owner else None
    if not channel:
        flash("Couldn't find that channel.", "error")
        return redirect(url_for("parent.account"))

    spec = CHANNELS.get(channel["type"], {})
    config = {}
    for key, _label, _t, _req, _ph, _help in spec.get("fields", []):
        config[key] = request.form.get(f"{channel['type']}__{key}", "").strip()

    allowed = set(events_for(owner["role"]))
    events = [e for e in request.form.getlist("events") if e in allowed]
    enabled = bool(request.form.get("enabled"))

    update_channel(db, owner["_id"], channel_id, config, events, enabled)
    flash(f"Updated {owner['name']}'s {spec.get('label', 'channel')}.", "success")
    return _owner_redirect(owner)


@bp.post("/channels/<owner_id>/<channel_id>/test")
@parent_required
def test_channel(owner_id, channel_id):
    db = get_db()
    owner = _channel_owner(db, owner_id)
    channel = get_channel(db, owner_id, channel_id) if owner else None
    if not channel:
        flash("Couldn't find that channel.", "error")
        return redirect(url_for("parent.account"))
    ok, detail = deliver(channel, "Questly test",
                         f"If {owner['name']} can read this, it's working.")
    flash("Test message sent." if ok else f"Test failed: {detail}",
          "success" if ok else "error")
    return _owner_redirect(owner)


@bp.post("/channels/<owner_id>/<channel_id>/delete")
@parent_required
def delete_channel(owner_id, channel_id):
    db = get_db()
    owner = _channel_owner(db, owner_id)
    if not owner:
        flash("Couldn't find that channel.", "error")
        return redirect(url_for("parent.account"))
    remove_channel(db, owner["_id"], channel_id)
    flash("Channel removed.", "info")
    return _owner_redirect(owner)


# ---------------------------------------------------------------------------
# your own account
# ---------------------------------------------------------------------------

@bp.get("/account")
@parent_required
def account():
    return render_template("parent/account.html",
                           me=g.user,
                           avatars=PARENT_AVATARS,
                           channels=CHANNELS,
                           events=events_for("parent"),
                           summary=channel_summary)


@bp.post("/account/profile")
@parent_required
def update_profile():
    ok, error = update_parent(
        get_db(), g.user["_id"],
        name=request.form.get("name", ""),
        email=request.form.get("email", ""),
        avatar=request.form.get("avatar") or None,
    )
    flash(error if error else "Saved.", "error" if error else "success")
    return redirect(url_for("parent.account"))


@bp.post("/account/password")
@parent_required
def change_password():
    db = get_db()
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")

    if not check_password(g.user, current):
        flash("Your current password wasn't right.", "error")
    elif new != confirm:
        flash("The two new passwords don't match.", "error")
    else:
        ok, error = set_parent_password(db, g.user["_id"], new)
        flash(error if error else "Password changed.",
              "error" if error else "success")
    return redirect(url_for("parent.account"))


@bp.post("/family/parents/<parent_id>/password")
@parent_required
def reset_parent_password(parent_id):
    """Either grown-up can reset the other's password. Confirming with your
    own password stops someone using an unlocked session to take over."""
    db = get_db()
    target = get_user(db, parent_id)

    if not target or target["role"] != "parent":
        flash("Couldn't find that account.", "error")
        return redirect(url_for("parent.family"))
    if target["_id"] == g.user["_id"]:
        return redirect(url_for("parent.account"))

    if not check_password(g.user, request.form.get("your_password", "")):
        flash("Confirm with your own password to reset someone else's.", "error")
        return redirect(url_for("parent.family"))

    ok, error = set_parent_password(db, target["_id"],
                                    request.form.get("new_password", ""))
    if error:
        flash(error, "error")
    else:
        flash(f"Set a new password for {target['name']}. Let them know.", "success")
    return redirect(url_for("parent.family"))


@bp.post("/family/parents/<parent_id>/delete")
@parent_required
def remove_parent(parent_id):
    db = get_db()
    target = get_user(db, parent_id)
    if not target or target["role"] != "parent":
        flash("Couldn't find that account.", "error")
    elif target["_id"] == g.user["_id"]:
        flash("You can't remove your own account while you're using it.", "warn")
    elif db.users.count_documents({"role": "parent"}) <= 1:
        flash("There has to be at least one grown-up account.", "warn")
    else:
        db.users.delete_one({"_id": target["_id"]})
        flash(f"Removed {target['name']}.", "info")
    return redirect(url_for("parent.family"))
