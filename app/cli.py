"""`flask` CLI commands for setting up and seeding a family."""

import click
from flask.cli import with_appcontext

from .db import ensure_indexes, get_db
from .models import adjust_points, create_kid, create_parent, list_kids
from .services.pets import ensure_pet


def register(app):
    app.cli.add_command(init_db_command)
    app.cli.add_command(create_parent_command)
    app.cli.add_command(seed_demo_command)
    app.cli.add_command(migrate_v2_command)


@click.command("init-db")
@with_appcontext
def init_db_command():
    """Create the indexes."""
    ensure_indexes(get_db())
    click.echo("Indexes ready.")


@click.command("migrate-v2")
@with_appcontext
def migrate_v2_command():
    """Idempotently add locale, pet, and lifetime fields to an existing DB."""
    db = get_db()
    ensure_indexes(db)
    kids = list(db.users.find({"role": "kid"}))
    parents = list(db.users.find({"role": "parent"}))
    for parent in parents:
        changes = {"status": "active", "auth_version": 0, "locale": "zh_CN"}
        missing = {key: value for key, value in changes.items() if key not in parent}
        if missing:
            db.users.update_one({"_id": parent["_id"]}, {"$set": missing})
    for kid in kids:
        changes = {"status": "active", "auth_version": 0, "locale": "zh_CN",
                   "points": int(kid.get("points", 0))}
        missing = {key: value for key, value in changes.items() if key not in kid}
        if "lifetime_points" not in kid:
            rows = db.transactions.find(
                {"kid_id": kid["_id"], "delta": {"$gt": 0},
                 "kind": {"$in": ["award", "quest", "parent_award"]}},
                {"delta": 1},
            )
            earned = sum(int(row.get("delta", 0)) for row in rows)
            missing["lifetime_points"] = earned if earned else int(kid.get("points", 0))
        if missing:
            db.users.update_one({"_id": kid["_id"]}, {"$set": missing})
        migrated = db.users.find_one({"_id": kid["_id"]})
        ensure_pet(db, migrated)
    click.echo(f"Migration complete: {len(parents)} parent(s), {len(kids)} child account(s).")


@click.command("create-parent")
@click.option("--name", prompt=True)
@click.option("--email", prompt=True)
@click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
@with_appcontext
def create_parent_command(name, email, password):
    """Add a grown-up account from the command line."""
    db = get_db()
    if db.users.find_one({"email": email.strip().lower()}):
        raise click.ClickException("That email is already registered.")
    create_parent(db, name, email, password)
    click.echo(f"Created parent account for {name}.")


@click.command("seed-demo")
@with_appcontext
def seed_demo_command():
    """Fill the shop and quest board with example content."""
    db = get_db()
    ensure_indexes(db)

    if not db.users.count_documents({"role": "parent"}, limit=1):
        create_parent(db, "Demo Parent", "parent@example.com", "password123")
        click.echo("Parent login: parent@example.com / password123")

    if not list_kids(db):
        for name, avatar, color, pin in [
            ("Ava", "\U0001f984", "#ff4d94", "1111"),
            ("Noah", "\U0001f996", "#28c8f5", None),
        ]:
            kid_id = create_kid(db, name, avatar, color, pin=pin)
            adjust_points(db, kid_id, 45, "Starting points", None, kind="award")
        click.echo("Added kids: Ava (PIN 1111) and Noah (no PIN)")

    rewards = [
        ("Extra 30 min screen time", "\U0001f4f1", 20, "Your choice of tablet, TV or console."),
        ("Pick tonight's dinner", "\U0001f355", 30, "Anything from the shortlist."),
        ("Stay up 30 min later", "\U0001f989", 40, "Weekends only!"),
        ("Trip to the park", "\U0001f333", 50, "With a scooter and a snack."),
        ("Cinema trip", "\U0001f37f", 120, "Popcorn included."),
        ("New book", "\U0001f4da", 150, "You choose it."),
    ]
    for title, emoji, cost, desc in rewards:
        db.rewards.update_one(
            {"title": title},
            {"$setOnInsert": {"title": title, "emoji": emoji, "cost": cost,
                              "description": desc, "stock": None, "active": True}},
            upsert=True,
        )

    quests = [
        ("Make your bed", "\U0001f6cf", 3, "daily"),
        ("Read for 20 minutes", "\U0001f4d6", 5, "daily"),
        ("Tidy your room", "\U0001f9f9", 8, "daily"),
        ("Help with dinner", "\U0001f957", 6, "daily"),
        ("Homework done", "✏️", 10, "daily"),
        ("Take the bins out", "\U0001f5d1️", 12, "weekly"),
        ("Be kind to your sibling", "\U0001f49b", 5, "daily"),
    ]
    for title, emoji, points, repeat in quests:
        db.quests.update_one(
            {"title": title},
            {"$setOnInsert": {"title": title, "emoji": emoji, "points": points,
                              "repeat": repeat, "assigned_to": [], "active": True}},
            upsert=True,
        )

    click.echo("Demo shop and quest board ready.")
