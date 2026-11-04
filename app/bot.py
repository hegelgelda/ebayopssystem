"""bot コンテナ：Discord Bot 常駐（F4）。

- outbox テーブルを拾って投稿する（#orders はフォーラムで1注文1スレッド）
- 通知の「対応済み」「担当を引き取る」ボタンを処理し、操作ログに残す
- ツール上の権限に合わせて Lv1 / Lv2 ロールを付け外しする（権限の正はツール側）
"""
from __future__ import annotations

import asyncio
import logging
import re
import signal
import sys

import discord
from discord.ext import tasks
from sqlalchemy import select

from app import audit
from app.config import get_config
from app.db import SessionLocal
from app.logging_setup import setup_logging
from app.models import Notification, Order, Outbox, User
from app.services import notify
from app.timeutil import utcnow

log = logging.getLogger("bot")
MAX_ATTEMPTS = 8


def channel_id(key: str) -> int | None:
    cfg = get_config()
    v = getattr(cfg, f"discord_channel_{key}", "") or ""
    return int(v) if v.isdigit() else None


def role_id(key: str) -> int | None:
    v = getattr(get_config(), f"discord_role_{key}", "") or ""
    return int(v) if v.isdigit() else None


def build_content(payload: dict) -> str:
    mentions = [f"<@{u}>" for u in payload.get("mention_user_ids", [])]
    for r in payload.get("mention_roles", []):
        rid = role_id(r)
        mentions.append(f"<@&{rid}>" if rid else f"@{r.upper()}")
    text = payload.get("content", "")
    return (" ".join(mentions) + "\n" + text).strip() if mentions else text


class NotificationButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ops:(?P<action>ack|claim):(?P<nid>\d+)"):
    def __init__(self, action: str, nid: int):
        label = "対応済み" if action == "ack" else "担当を引き取る"
        style = discord.ButtonStyle.success if action == "ack" else discord.ButtonStyle.primary
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"ops:{action}:{nid}"))
        self.action, self.nid = action, nid

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(match["action"], int(match["nid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.discord_user_id == str(interaction.user.id),
                                                User.is_active.is_(True)))
            if user is None:
                await interaction.response.send_message("ツールに登録されていないため操作できません。", ephemeral=True)
                return
            n = db.get(Notification, self.nid)
            if n is None:
                await interaction.response.send_message("通知が見つかりません。", ephemeral=True)
                return
            actor = audit.Actor.bot(user)
            if self.action == "claim" and n.target_type == "order":
                o = db.get(Order, n.target_id)
                if o:
                    o.assignee_id = user.id
                    o.version += 1
                    audit.log(db, actor, "order", o.id, "order.claim", after={"assignee_id": user.id, "via": "discord"})
            changed = notify.acknowledge(db, n, user, actor, via=f"discord-{self.action}")
            db.commit()
        msg = (f"✋ {user.display_name} さんが担当を引き取りました。" if self.action == "claim"
               else (f"✅ {user.display_name} さんが対応済みにしました。" if changed else "既に対応済みです。"))
        await interaction.response.send_message(msg, ephemeral=not changed and self.action == "ack")


def make_view(payload: dict) -> discord.ui.View | None:
    nid = payload.get("notification_id")
    link = payload.get("link")
    if not (payload.get("buttons") and nid) and not link:
        return None
    v = discord.ui.View(timeout=None)
    if payload.get("buttons") and nid:
        v.add_item(NotificationButton("ack", nid))
        if payload.get("order_id"):
            v.add_item(NotificationButton("claim", nid))
    if link and link.startswith("https://"):
        v.add_item(discord.ui.Button(label="Webで開く", url=link))
    return v


class OpsBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default(),
                         allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False))

    async def setup_hook(self) -> None:
        self.add_dynamic_items(NotificationButton)
        self.outbox_loop.start()
        self.role_sync_loop.start()

    async def on_ready(self):
        log.info("bot ready as %s", self.user)

    async def _channel(self, key: str):
        cid = channel_id(key)
        if not cid:
            raise RuntimeError(f"DISCORD_CHANNEL_{key.upper()} が未設定です")
        return self.get_channel(cid) or await self.fetch_channel(cid)

    async def _order_thread(self, db, order_id: int, first_content: str, view):
        o = db.get(Order, order_id)
        if o is None:
            raise RuntimeError("order not found")
        if o.discord_thread_id:
            th = self.get_channel(int(o.discord_thread_id)) or await self.fetch_channel(int(o.discord_thread_id))
            if getattr(th, "archived", False):
                await th.edit(archived=False)
            return th, None
        ch = await self._channel("orders")
        it = o.item
        name = f"#{o.id} {it.card_name_en or it.card_name_jp or it.model_id} cert{it.cert_number}"[:95]
        if isinstance(ch, discord.ForumChannel):
            kwargs = {"name": name, "content": first_content}
            if view:
                kwargs["view"] = view
            res = await ch.create_thread(**kwargs)
            th, msg = res.thread, res.message
        else:
            msg = await ch.send(first_content, view=view) if view else await ch.send(first_content)
            th = await msg.create_thread(name=name)
        o.discord_thread_id = str(th.id)
        db.commit()
        return th, msg

    async def _post(self, db, payload: dict) -> str | None:
        content = build_content(payload)[:1990]
        view = make_view(payload)
        if payload.get("order_id") and payload.get("channel") == "orders":
            th, first = await self._order_thread(db, payload["order_id"], content, view)
            if first is not None:
                return str(first.id)
            msg = await th.send(content, view=view) if view else await th.send(content)
            return str(msg.id)
        ch = await self._channel(payload.get("channel", "system"))
        msg = await ch.send(content, view=view) if view else await ch.send(content)
        return str(msg.id)

    async def sync_roles(self, db) -> None:
        cfg = get_config()
        if not cfg.discord_guild_id.isdigit():
            return
        guild = self.get_guild(int(cfg.discord_guild_id)) or await self.fetch_guild(int(cfg.discord_guild_id))
        r1, r2 = role_id("lv1"), role_id("lv2")
        roles = {k: guild.get_role(v) for k, v in (("lv1", r1), ("lv2", r2)) if v}
        for u in db.scalars(select(User)):
            try:
                m = await guild.fetch_member(int(u.discord_user_id))
            except discord.NotFound:
                continue
            want = set()
            if u.is_active:
                want.add("lv2" if u.level >= 2 else "lv1")
            for key, role in roles.items():
                if role is None:
                    continue
                has = role in m.roles
                if key in want and not has:
                    await m.add_roles(role, reason="ツールの権限に同期")
                    audit.log(db, audit.Actor("bot"), "user", u.id, "discord.role_add", after={"role": key})
                elif key not in want and has:
                    await m.remove_roles(role, reason="ツールの権限に同期")
                    audit.log(db, audit.Actor("bot"), "user", u.id, "discord.role_remove", after={"role": key})
        db.commit()

    @tasks.loop(seconds=3)
    async def outbox_loop(self):
        with SessionLocal() as db:
            rows = db.scalars(select(Outbox).where(Outbox.status == "pending").order_by(Outbox.id).limit(20)).all()
            for ob in rows:
                ob.attempts += 1
                try:
                    if ob.kind == "post":
                        mid = await self._post(db, ob.payload)
                        nid = ob.payload.get("notification_id")
                        if nid and mid:
                            n = db.get(Notification, nid)
                            if n and not n.discord_message_id:
                                n.discord_message_id = mid
                    elif ob.kind == "sync_roles":
                        await self.sync_roles(db)
                    ob.status, ob.sent_at, ob.error = "sent", utcnow(), None
                except Exception as e:  # noqa: BLE001
                    log.warning("outbox %s failed: %s", ob.id, e)
                    ob.error = str(e)[:500]
                    if ob.attempts >= MAX_ATTEMPTS:
                        ob.status = "failed"
                db.commit()

    @tasks.loop(minutes=30)
    async def role_sync_loop(self):
        with SessionLocal() as db:
            try:
                await self.sync_roles(db)
            except Exception as e:  # noqa: BLE001
                log.warning("role sync failed: %s", e)

    @outbox_loop.before_loop
    @role_sync_loop.before_loop
    async def _wait(self):
        await self.wait_until_ready()


def main() -> None:
    cfg = get_config()
    cfg.ensure_dirs()
    setup_logging("bot")
    # コンテナの PID 1 として SIGTERM で確実に終了する（docker compose stop / 再起動用）
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if not cfg.discord_bot_token:
        log.warning("DISCORD_BOT_TOKEN が未設定のため、Botは待機します（通知は outbox に溜まります）")
        while True:
            asyncio.run(asyncio.sleep(3600))
    OpsBot().run(cfg.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()
