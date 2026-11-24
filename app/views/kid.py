from flask import (Blueprint, flash, g, redirect, render_template, request,
                   session, url_for)

from ..db import get_db
from ..models import (THEMES, annotate_rewards, claim_quest, get_quest,
                      period_key, toggle_subtask,
                      get_reward, goal_for, history_for, list_rewards,
                      quests_for_kid, redeem, redemptions_for, set_kid_goal,
                      set_kid_theme, theme_for)
from ..models import get_user, list_parents
from ..notify import compose, fanout, mark_all_read, notify_many, recent
from ..services.pets import active_pet, customize_pet, pet_history, status_for
from ..services.pets import DEFAULT_PET_RULES
from .helpers import kid_required, local_now

bp = Blueprint("kid", __name__, url_prefix="/me")


@bp.get("/")
@kid_required
def home():
    db = get_db()
    kid = g.user
    now = local_now()
    quests = quests_for_kid(db, kid, now)
    rewards = annotate_rewards(db, list_rewards(db), kid, now)

    goal, chosen = goal_for(db, kid, rewards)
    balance = kid.get("points", 0)

    return render_template(
        "kid/home.html",
        kid=kid,
        quests=quests,
        open_quests=[q for q in quests if q["state"] == "open"],
        goal=goal,
        goal_chosen=chosen,
        goal_reached=bool(goal and balance >= goal["cost"]),
        affordable=[r for r in rewards if r["cost"] <= balance and not r["sold_out"]],
        history=history_for(db, kid["_id"], limit=8),
        pet=active_pet(db, kid["_id"]),
    )


@bp.get("/shop")
@kid_required
def shop():
    db = get_db()
    return render_template(
        "kid/shop.html",
        kid=g.user,
        rewards=annotate_rewards(db, list_rewards(db), g.user, local_now()),
        goal_id=g.user.get("goal_reward_id"),
        pending=[r for r in redemptions_for(db, g.user["_id"], limit=20)
                 if r["status"] == "pending"],
    )


@bp.post("/shop/<reward_id>/buy")
@kid_required
def buy(reward_id):
    db = get_db()
    reward = get_reward(db, reward_id)
    if not reward or not reward.get("active"):
        flash("该奖励当前不可兑换。", "error")
        return redirect(url_for("kid.shop"))

    _, error = redeem(db, g.user, reward, local_now(),
                      request.form.get("idempotency_key"))
    if error:
        flash(error, "error")
    else:
        left = (get_user(db, g.user["_id"]) or {}).get("points", 0)
        notify_many(
            db, list_parents(db), "approval_waiting",
            f"{reward.get('emoji', '')} {g.user['name']} 兑换了 {reward['title']}".strip(),
            compose(
                f"花费：{reward['cost']} 积分",
                f"{g.user['name']} 还剩 {left} 积分",
                reward.get("description"),
                "请交付奖励，然后在 Questly 中标记为已交付。",
            ))
        session["celebrate"] = f"你兑换了 {reward['title']}！请让家长交付给你。"
    return redirect(url_for("kid.shop"))


@bp.post("/quests/<quest_id>/done")
@kid_required
def finish_quest(quest_id):
    db = get_db()
    quest = get_quest(db, quest_id)
    if not quest or not quest.get("active"):
        flash("该任务已下架。", "error")
        return redirect(url_for("kid.home"))

    assigned = quest.get("assigned_to") or []
    if assigned and g.user["_id"] not in assigned:
        flash("该任务未分配给你。", "error")
        return redirect(url_for("kid.home"))

    _, error = claim_quest(db, quest, g.user, local_now(),
                           request.form.get("idempotency_key"))
    if error:
        flash(error, "warn")
    else:
        notify_many(
            db, list_parents(db), "approval_waiting",
            f"{quest.get('emoji', '')} {g.user['name']} finished {quest['title']}".strip(),
            compose(
                f"Quest: {quest['title']} ({quest.get('repeat', 'daily')})",
                f"Worth {quest['points']} points",
                f"{g.user['name']} has {g.user.get('points', 0)} points right now",
                "Approve it in Questly to pay out.",
            ))
        session["celebrate"] = f"Nice one! {quest['points']} points on the way once it's checked."
    return redirect(url_for("kid.home"))


@bp.post("/goal/<reward_id>")
@kid_required
def set_goal(reward_id):
    """Save up for a particular reward. Posting 'clear' goes back to automatic."""
    db = get_db()
    if reward_id == "clear":
        set_kid_goal(db, g.user["_id"], None)
        flash("已恢复显示最近可兑换的奖励。", "info")
        return redirect(request.form.get("next") or url_for("kid.shop"))

    reward = get_reward(db, reward_id)
    if not reward or not reward.get("active"):
        flash("该奖励当前不可兑换。", "error")
    else:
        set_kid_goal(db, g.user["_id"], reward["_id"])
        flash(f"已设定存积分目标：{reward['emoji']} {reward['title']}。", "success")
    return redirect(request.form.get("next") or url_for("kid.shop"))


@bp.get("/theme")
@kid_required
def theme():
    return render_template("kid/theme.html", kid=g.user, themes=THEMES,
                           current=theme_for(g.user)["key"])


@bp.post("/theme")
@kid_required
def choose_theme():
    if set_kid_theme(get_db(), g.user["_id"], request.form.get("theme", "")):
        session["celebrate"] = "Nice pick! Your colours are updated."
    else:
        flash("请选择有效的主题。", "error")
    return redirect(url_for("kid.theme"))


@bp.post("/quests/<quest_id>/step/<subtask_id>")
@kid_required
def tick_step(quest_id, subtask_id):
    """Tick or untick one step of a quest."""
    db = get_db()
    quest = get_quest(db, quest_id)
    if not quest or not quest.get("active"):
        flash("该任务已下架。", "error")
        return redirect(url_for("kid.home"))

    assigned = quest.get("assigned_to") or []
    if assigned and g.user["_id"] not in assigned:
        flash("该任务未分配给你。", "error")
        return redirect(url_for("kid.home"))

    key = period_key(quest.get("repeat", "daily"), local_now())
    toggle_subtask(db, quest, g.user, key, subtask_id)
    return redirect(url_for("kid.home") + f"#quest-{quest['_id']}")


@bp.get("/news")
@kid_required
def news():
    db = get_db()
    items = recent(db, g.user["_id"], limit=50)
    mark_all_read(db, g.user["_id"])
    return render_template("kid/news.html", kid=g.user, items=items)


@bp.get("/stuff")
@kid_required
def stuff():
    db = get_db()
    return render_template(
        "kid/stuff.html",
        kid=g.user,
        history=history_for(db, g.user["_id"], limit=100),
        redemptions=redemptions_for(db, g.user["_id"], limit=50),
    )


@bp.get("/pet")
@kid_required
def pet():
    db = get_db()
    companion = active_pet(db, g.user["_id"])
    if companion:
        settings = db.family_settings.find_one({"_id": "family"}) or {}
        companion["status"] = status_for(
            companion.get("hp", 0),
            settings.get("pet_rules", DEFAULT_PET_RULES),
        )
    return render_template("kid/pet.html", kid=g.user, pet=companion,
                           history=pet_history(db, g.user["_id"], limit=50))


@bp.post("/pet/customize")
@kid_required
def customize_companion():
    ok = customize_pet(get_db(), g.user, request.form.get("name", ""),
                       request.form.get("species", "cat"))
    flash("宠物外观已更新。" if ok else "还没有可修改的宠物。",
          "success" if ok else "error")
    return redirect(url_for("kid.pet"))
