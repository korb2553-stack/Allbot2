"""
Discord Bot (discord.py 2.x)
 - ล็อคไมค์ (server mute) ปลดได้เฉพาะยศที่กำหนด
 - /ซ่อน /แสดง  ซ่อน-แสดงห้องเสียง
 - /ล็อค /ปลด   ปิด-เปิดการเชื่อมต่อห้อง + Snapshot รายชื่อคนในห้อง
 - !bl <ID> / !unbl <ID> (หรือ /bl /unbl)  Blacklist: พิมพ์แล้วบอทเตือนและลบข้อความ
 - !ขัง <ID> / !ปล่อย <ID>  ขังไว้ในห้องที่ลงล่าสุด (enforce เมื่อลงห้อง)

ติดตั้ง:  pip install -U discord.py
รัน:      วางโทเคนในไฟล์ token.txt (หรือตั้ง DISCORD_TOKEN) แล้ว python main.py
หมายเหตุ: ถ้ามีไฟล์ modlog.py / protect.py วางข้างไฟล์นี้ บอทจะโหลดให้อัตโนมัติ
"""
import asyncio
import datetime
import json
import os
import re
import time
import traceback
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

DATA_FILE = Path("data.json")
def _read_token() -> str:
    """อ่านโทเคนจากตัวแปร DISCORD_TOKEN หรือไฟล์ token.txt (ไฟล์นี้ไม่ต้องอัปขึ้น GitHub)"""
    t = os.getenv("DISCORD_TOKEN")
    if not t:
        f = Path(__file__).with_name("token.txt")
        if f.exists():
            t = f.read_text(encoding="utf-8").strip()
    if not t or "ใส่โทเคน" in t:
        # ยังไม่มีโทเคน -> ถามตอนรัน แล้วบันทึกลง token.txt ให้เอง (ไม่ต้องแก้ไฟล์)
        t = input("วางโทเคนบอทตรงนี้แล้วกด Enter: ").strip()
        if not t:
            raise SystemExit("ไม่ได้ใส่โทเคน")
        Path(__file__).with_name("token.txt").write_text(t + "\n", encoding="utf-8")
    return t


TOKEN = _read_token()
PREFIX_RE = re.compile(r"^!(ขัง|ปล่อย)\s+<?@?!?(\d{15,20})>?")
BL_RE = re.compile(r"^!(bl|unbl)\s+<?@?!?(\d{15,20})>?", re.IGNORECASE)  # พิมพ์เล็ก/ใหญ่ได้


# ---------- เก็บข้อมูลลงไฟล์ ----------
def _default():
    return {
        "allowed_roles": [],   # ยศที่ปลดไมค์/ใช้คำสั่งจัดการได้
        "muted": [],           # คนที่ถูกล็อคไมค์
        "jail_channel": None,  # ห้องขังสำรอง (ใช้เมื่อไม่รู้ว่าลงห้องไหนล่าสุด)
        "jailed": {},          # คนที่ถูกขัง {user_id: ห้องที่ขังไว้}
        "blacklist": [],       # คนที่ติด Blacklist
        "saved": {},           # สิทธิ์เดิมของห้องก่อนซ่อน/ล็อค
    }


def load():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {}


def save():
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


data = load()
bl_active = {}   # (guild_id, user_id) -> กำลังแจ้งเตือนอยู่ (กันบอทตอบรัว)
last_voice = {}  # (guild_id, user_id) -> ห้องเสียงล่าสุดที่บอทเห็นว่าเข้า


def g(guild_id: int) -> dict:
    d = data.setdefault(str(guild_id), {})
    for k, v in _default().items():
        d.setdefault(k, v)
    if isinstance(d["jailed"], list):  # ข้อมูลเก่า
        d["jailed"] = {str(u): d.get("jail_channel") for u in d["jailed"]}
    return d


# ---------- บอท ----------
intents = discord.Intents.default()
intents.members = True          # ต้องเปิด Server Members Intent ใน Developer Portal
intents.voice_states = True
intents.message_content = True  # ต้องเปิด Message Content Intent ใน Developer Portal (สำหรับ !ขัง / !ปล่อย / !bl)

bot = None  # ถูกสร้างใหม่ทุกครั้งที่ล็อกอินใน make_bot()


async def _tell_error(interaction: discord.Interaction, error: Exception):
    """แสดงข้อผิดพลาดให้คนกดเห็นในดิสคอร์ด (และพิมพ์ลง Console) แทนที่จะขึ้นแค่ 'ไม่ตอบสนอง'"""
    traceback.print_exception(type(error), error, error.__traceback__)
    msg = f"⚠️ เกิดข้อผิดพลาด: `{type(error).__name__}: {error}`"[:1900]
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


async def _view_error(self, interaction, error, item):
    await _tell_error(interaction, error)


async def _modal_error(self, interaction, error):
    await _tell_error(interaction, error)


discord.ui.View.on_error = _view_error
discord.ui.Modal.on_error = _modal_error


class AdminBot(commands.Bot):
    async def on_tree_error(self, interaction, error):
        await _tell_error(interaction, error)

    async def setup_hook(self):
        self.tree.on_error = self.on_tree_error
        for cmd in COMMANDS:  # สแลชคำสั่งทั้งหมดในไฟล์นี้
            self.tree.add_command(cmd)
        for ext in ("modlog", "protect"):  # ส่วนเสริม (ถ้ามีไฟล์)
            try:
                await self.load_extension(ext)
            except commands.ExtensionNotFound:
                print(f"⚠️ ไม่พบส่วนเสริม {ext}.py ข้ามไป")
        await self.tree.sync()


def can_unmute(member: discord.Member) -> bool:
    """เจ้าของเซิร์ฟเวอร์ หรือมียศที่ตั้งไว้เท่านั้น"""
    if member.id == member.guild.owner_id:
        return True
    allowed = set(g(member.guild.id)["allowed_roles"])
    return any(r.id in allowed for r in member.roles)


def is_staff(member: discord.Member) -> bool:
    """ใช้คำสั่งจัดการห้อง/ขัง: ยศที่ตั้งไว้ หรือแอดมิน"""
    return can_unmute(member) or member.guild_permissions.administrator


# ---------- ระบบล็อคไมค์ ----------
async def find_executor(guild: discord.Guild, target: discord.Member, mute_value: bool):
    """หาว่าใครเป็นคนกดปิด/เปิดไมค์ จาก Audit Log"""
    await asyncio.sleep(1)
    now = datetime.datetime.now(datetime.timezone.utc)
    async for entry in guild.audit_logs(limit=10, action=discord.AuditLogAction.member_update):
        if entry.target is None or entry.target.id != target.id:
            continue
        if (now - entry.created_at).total_seconds() > 15:
            continue
        if getattr(entry.after, "mute", None) == mute_value:
            return entry.user
    return None


async def send_warning(member: discord.Member, executor: discord.abc.User, channel):
    text = (
        f"{executor.mention} ⚠️ คุณไม่สามารถเปิดไมค์ให้ {member.mention} ได้ "
        f"เนื่องจากแอดมินเป็นคนปิดไว้ กรุณาติดต่อแอดมินเพื่อทำการปลด"
    )
    try:
        await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))
    except discord.HTTPException:
        pass


async def on_voice_state_update(member, before, after):
    gd = g(member.guild.id)
    muted = gd["muted"]

    # จำห้องล่าสุดที่เข้า (เฉพาะคนที่ยังไม่ถูกขัง)
    jail_id = gd["jailed"].get(str(member.id))
    if jail_id is None:
        if after.channel:
            last_voice[(member.guild.id, member.id)] = after.channel.id
    elif after.channel and after.channel.id != jail_id:
        # ขังอยู่ แล้วเข้า/ย้ายไปห้องอื่น -> ลากกลับห้องที่ถูกขัง
        jail = member.guild.get_channel(jail_id)
        if jail:
            try:
                await member.move_to(jail, reason="ขัง (enforce เมื่อลงห้อง)")
            except discord.HTTPException:
                pass

    # เข้าห้องมาใหม่ + อยู่ในลิสต์ล็อคไมค์ -> ปิดไมค์ซ้ำ
    if before.channel is None and after.channel is not None:
        if member.id in muted and not after.mute:
            try:
                await member.edit(mute=True, reason="ล็อคไมค์ (enforce เมื่อลงห้อง)")
            except discord.HTTPException:
                pass
        return

    if before.mute == after.mute or after.channel is None:
        return

    executor = await find_executor(member.guild, member, after.mute)
    if executor is None or executor.id == bot.user.id:
        return

    if after.mute:
        if member.id not in muted:
            muted.append(member.id)
            save()
        return

    if member.id not in muted:
        return
    executor_member = member.guild.get_member(executor.id)
    if executor_member and can_unmute(executor_member):
        muted.remove(member.id)
        save()
    else:
        try:
            await member.edit(mute=True, reason="ผู้ปลดไม่มียศที่อนุญาต")
        except discord.HTTPException:
            pass
        await send_warning(member, executor, after.channel)


# ---------- ซ่อน/แสดง, ล็อค/ปลด ห้อง ----------
def enc(v):
    return "t" if v is True else "f" if v is False else "n"


def dec(s):
    return True if s == "t" else False if s == "f" else None


def target_roles(channel, gd):
    """@everyone + ยศที่มี overwrite ในห้อง (ยกเว้นยศทีมงาน และยศของบอท)"""
    staff = set(gd["allowed_roles"])
    roles = [channel.guild.default_role]
    for t in channel.overwrites:
        if isinstance(t, discord.Role) and t != channel.guild.default_role \
                and t.id not in staff and not t.managed:
            roles.append(t)
    return roles


async def restrict(channel, kind: str, perm: str, gd: dict):
    saved = gd["saved"].setdefault(f"{kind}:{channel.id}", {})
    roles = target_roles(channel, gd)
    ok = 0
    for role in roles:
        ow = channel.overwrites_for(role)
        if str(role.id) not in saved:
            saved[str(role.id)] = enc(getattr(ow, perm))
        setattr(ow, perm, False)
        try:
            await channel.set_permissions(role, overwrite=ow, reason=f"{kind} ห้อง")
            ok += 1
        except discord.HTTPException:
            pass
    save()
    return ok, len(roles)


async def restore(channel, kind: str, perm: str, gd: dict):
    saved = gd["saved"].pop(f"{kind}:{channel.id}", None)
    if saved is None:
        saved = {str(channel.guild.default_role.id): "n"}
    ok = 0
    for rid, s in saved.items():
        role = channel.guild.get_role(int(rid))
        if role is None:
            continue
        ow = channel.overwrites_for(role)
        setattr(ow, perm, dec(s))
        try:
            await channel.set_permissions(
                role, overwrite=None if ow.is_empty() else ow, reason=f"คืนสิทธิ์ {kind}"
            )
            ok += 1
        except discord.HTTPException:
            pass
    save()
    return ok, len(saved)


def snapshot_embed(channel, action: str) -> discord.Embed:
    members = channel.members
    blocks = []
    for m in members[:15]:
        vs = m.voice
        mic = vs.self_mute or vs.mute
        deaf = vs.self_deaf or vs.deaf
        blocks.append(
            f"👤 **{m.display_name}**\n"
            f"ID: `{m.id}`\n"
            f"{'🔇' if mic else '🎤'} ไมค์: {'ปิด' if mic else 'เปิด'} | "
            f"{'🔇' if deaf else '🔊'} หูฟัง: {'ปิด' if deaf else 'เปิด'}\n"
            f"กล้อง: {'เปิด' if vs.self_video else 'ปิด'} | "
            f"สตรีม: {'เปิด' if vs.self_stream else 'ปิด'}"
        )
    if len(members) > 15:
        blocks.append(f"... และอีก {len(members) - 15} คน")
    desc = f"การกระทำ: {action}\nสมาชิกทั้งหมด: {len(members)} คน"
    if blocks:
        desc += "\n\n" + "\n\n".join(blocks)
    return discord.Embed(
        title=f"📸 Snapshot: {channel.name}",
        description=desc,
        color=0x4A90D9,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )


async def room_action(i: discord.Interaction, channel, kind, perm, do_restrict, text, snap):
    if not is_staff(i.user):
        return await i.response.send_message("⚠️ คุณไม่มียศที่ใช้คำสั่งนี้", ephemeral=True)
    ch = channel or (i.user.voice.channel if i.user.voice else None)
    if ch is None:
        return await i.response.send_message(
            "⚠️ เข้าห้องเสียงก่อน หรือระบุห้องที่ต้องการ", ephemeral=True
        )
    await i.response.defer()
    gd = g(i.guild_id)
    fn = restrict if do_restrict else restore
    ok, total = await fn(ch, kind, perm, gd)
    msg = f"{text} {ch.mention} เรียบร้อยแล้ว! ({ok}/{total} roles)"
    if snap:
        await i.followup.send(msg, embed=snapshot_embed(ch, snap))
    else:
        await i.followup.send(msg)


ROOM_DESC = "ห้องเสียง (ไม่ใส่ = ห้องที่คุณอยู่)"


@app_commands.command(name="ซ่อน", description="ซ่อนห้องเสียง")
@app_commands.guild_only()
@app_commands.describe(channel=ROOM_DESC)
async def hide(i: discord.Interaction, channel: Optional[discord.VoiceChannel] = None):
    await room_action(i, channel, "hide", "view_channel", True, "🙈 ซ่อนห้อง", None)


@app_commands.command(name="แสดง", description="แสดงห้องเสียง")
@app_commands.guild_only()
@app_commands.describe(channel=ROOM_DESC)
async def show(i: discord.Interaction, channel: Optional[discord.VoiceChannel] = None):
    await room_action(i, channel, "hide", "view_channel", False, "👁️ แสดงห้อง", None)


@app_commands.command(name="ล็อค", description="ปิดการเชื่อมต่อห้องเสียง + Snapshot")
@app_commands.guild_only()
@app_commands.describe(channel=ROOM_DESC)
async def lock(i: discord.Interaction, channel: Optional[discord.VoiceChannel] = None):
    await room_action(i, channel, "lock", "connect", True,
                      "🔒 ปิดการเชื่อมต่อในห้อง", "ล็อคห้อง")


@app_commands.command(name="ปลด", description="เปิดการเชื่อมต่อห้องเสียง + Snapshot")
@app_commands.guild_only()
@app_commands.describe(channel=ROOM_DESC)
async def unlock(i: discord.Interaction, channel: Optional[discord.VoiceChannel] = None):
    await room_action(i, channel, "lock", "connect", False,
                      "🔓 เปิดการเชื่อมต่อในห้อง", "ปลดล็อคห้อง")


# ---------- Blacklist ----------
BL_WARN = "{} ⚠️ คุณติดแบล็คลิส กรุณารอแอดมินหรือคนมีอำนาจปลด 🖤"


def bl_toggle(guild: discord.Guild, uid: int, on: bool) -> str:
    gd = g(guild.id)
    if on:
        member = guild.get_member(uid)
        if member and is_staff(member):
            return "⚠️ ไม่สามารถ Blacklist ทีมงานได้"
        if uid in gd["blacklist"]:
            return f"<@{uid}> อยู่ใน Blacklist แล้ว"
        gd["blacklist"].append(uid)
        save()
        return f"🖤 เพิ่ม <@{uid}> เข้า Blacklist แล้ว"
    if uid not in gd["blacklist"]:
        return f"<@{uid}> ไม่ได้อยู่ใน Blacklist"
    gd["blacklist"].remove(uid)
    save()
    return f"🤍 ปลด Blacklist <@{uid}> แล้ว"


async def _try_delete(m):
    try:
        await m.delete()
    except discord.HTTPException:
        pass


async def handle_blacklisted(msg: discord.Message):
    """คนติด BL พิมพ์ -> ลบข้อความทันที + แจ้งในแชท (แจ้งครั้งเดียวต่อ 10 วิ กันสแปม)"""
    key = (msg.guild.id, msg.author.id)
    await _try_delete(msg)
    if key in bl_active:
        return
    bl_active[key] = True
    try:
        warn = await msg.channel.send(
            BL_WARN.format(msg.author.mention),
            allowed_mentions=discord.AllowedMentions(users=True),
        )
    except discord.HTTPException:
        bl_active.pop(key, None)
        return
    await asyncio.sleep(10)
    bl_active.pop(key, None)
    await _try_delete(warn)  # ลบข้อความแจ้งเตือนของบอทหลัง 10 วิ ให้แชทไม่รก


# ---------- ขัง / ปล่อย ----------
async def handle_message(msg: discord.Message):
    if msg.author.bot or not msg.guild:
        return

    # คนติด Blacklist (ทีมงานได้รับการยกเว้น)
    if msg.author.id in g(msg.guild.id)["blacklist"] and not is_staff(msg.author):
        return await handle_blacklisted(msg)

    bm = BL_RE.match(msg.content)
    if bm:
        if not is_staff(msg.author):
            return await msg.reply("⚠️ คุณไม่มียศที่ใช้คำสั่งนี้", mention_author=False)
        text = bl_toggle(msg.guild, int(bm.group(2)), bm.group(1).lower() == "bl")
        return await msg.reply(
            text, allowed_mentions=discord.AllowedMentions.none(), mention_author=False
        )

    m = PREFIX_RE.match(msg.content)
    if not m:
        return
    if not is_staff(msg.author):
        return await msg.reply("⚠️ คุณไม่มียศที่ใช้คำสั่งนี้", mention_author=False)

    cmd, uid = m.group(1), int(m.group(2))
    gd = g(msg.guild.id)
    member = msg.guild.get_member(uid)
    if member is None:
        try:
            member = await msg.guild.fetch_member(uid)
        except discord.HTTPException:
            return await msg.reply("⚠️ ไม่พบสมาชิก ID นี้ในเซิร์ฟเวอร์", mention_author=False)

    quiet = discord.AllowedMentions.none()
    if cmd == "ขัง":
        # ขังในห้องที่ลงล่าสุด: ห้องที่อยู่ตอนนี้ > ห้องล่าสุดที่บอทเห็น > ห้องขังสำรอง
        if member.voice and member.voice.channel:
            room_id = member.voice.channel.id
        else:
            room_id = last_voice.get((msg.guild.id, uid)) or gd["jail_channel"]
        jail = msg.guild.get_channel(room_id) if room_id else None
        if jail is None:
            return await msg.reply(
                "⚠️ ไม่พบห้องที่คนนี้ลงล่าสุด (ให้เขาเข้าห้องเสียงก่อน หรือตั้งห้องสำรองด้วย /ตั้งห้องขัง)",
                mention_author=False,
            )
        gd["jailed"][str(uid)] = jail.id
        save()
        await msg.reply(
            f"🔒 ทำการขัง {member.mention} → {jail.mention} (enforce เมื่อลงห้อง)",
            allowed_mentions=quiet, mention_author=False,
        )
        await msg.add_reaction("🔒")
    else:
        if gd["jailed"].pop(str(uid), None) is not None:
            save()
        await msg.reply(
            f"🔓 ทำการปล่อย {member.mention} แล้ว",
            allowed_mentions=quiet, mention_author=False,
        )
        await msg.add_reaction("🔓")


async def on_message(msg: discord.Message):
    if msg.author.bot:
        return
    consumed = bool(msg.guild) and (
        (msg.author.id in g(msg.guild.id)["blacklist"] and not is_staff(msg.author))
        or BL_RE.match(msg.content)
        or PREFIX_RE.match(msg.content)
    )
    await handle_message(msg)
    if not consumed:
        await bot.process_commands(msg)  # ให้คำสั่ง ! ของส่วนเสริมยังทำงาน


@app_commands.command(name="ตั้งห้องขัง", description="ตั้งห้องขังสำรอง (ใช้เมื่อไม่รู้ว่าคนที่โดนขังลงห้องไหนล่าสุด)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def set_jail(i: discord.Interaction, channel: discord.VoiceChannel):
    g(i.guild_id)["jail_channel"] = channel.id
    save()
    await i.response.send_message(f"✅ ตั้งห้องขังเป็น {channel.mention} แล้ว", ephemeral=True)


@app_commands.command(name="bl", description="เพิ่มเข้า Blacklist")
@app_commands.guild_only()
async def bl_add(i: discord.Interaction, user: discord.User):
    if not is_staff(i.user):
        return await i.response.send_message("⚠️ คุณไม่มียศที่ใช้คำสั่งนี้", ephemeral=True)
    await i.response.send_message(
        bl_toggle(i.guild, user.id, True), allowed_mentions=discord.AllowedMentions.none()
    )


@app_commands.command(name="unbl", description="ปลดออกจาก Blacklist")
@app_commands.guild_only()
async def bl_remove(i: discord.Interaction, user: discord.User):
    if not is_staff(i.user):
        return await i.response.send_message("⚠️ คุณไม่มียศที่ใช้คำสั่งนี้", ephemeral=True)
    await i.response.send_message(
        bl_toggle(i.guild, user.id, False), allowed_mentions=discord.AllowedMentions.none()
    )


# ---------- ตั้งค่ายศ ----------
roles_group = app_commands.Group(
    name="unmute_roles",
    description="ตั้งค่ายศที่ปลดไมค์ / จัดการห้อง / ขังได้",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


@roles_group.command(name="add", description="เพิ่มยศ")
async def roles_add(i: discord.Interaction, role: discord.Role):
    gd = g(i.guild_id)
    if role.id in gd["allowed_roles"]:
        return await i.response.send_message(f"{role.mention} อยู่ในลิสต์แล้ว", ephemeral=True)
    gd["allowed_roles"].append(role.id)
    save()
    await i.response.send_message(f"✅ เพิ่ม {role.mention} แล้ว", ephemeral=True)


@roles_group.command(name="remove", description="ลบยศ")
async def roles_remove(i: discord.Interaction, role: discord.Role):
    gd = g(i.guild_id)
    if role.id not in gd["allowed_roles"]:
        return await i.response.send_message("ยศนี้ไม่ได้อยู่ในลิสต์", ephemeral=True)
    gd["allowed_roles"].remove(role.id)
    save()
    await i.response.send_message(f"🗑️ ลบ {role.mention} แล้ว", ephemeral=True)


@roles_group.command(name="list", description="ดูยศทั้งหมด")
async def roles_list(i: discord.Interaction):
    ids = g(i.guild_id)["allowed_roles"]
    text = "\n".join(f"<@&{r}>" for r in ids) if ids else "ยังไม่ได้ตั้งค่า (มีแค่เจ้าของเซิร์ฟเวอร์/แอดมิน)"
    await i.response.send_message(f"**ยศที่ใช้งานได้**\n{text}", ephemeral=True)




@app_commands.command(name="vmute", description="ปิดไมค์แบบล็อค (ปลดได้เฉพาะยศที่กำหนด)")
@app_commands.guild_only()
async def vmute(i: discord.Interaction, member: discord.Member):
    if not can_unmute(i.user):
        return await i.response.send_message("⚠️ คุณไม่มียศที่ใช้คำสั่งนี้", ephemeral=True)
    gd = g(i.guild_id)
    if member.id not in gd["muted"]:
        gd["muted"].append(member.id)
        save()
    if member.voice:
        await member.edit(mute=True, reason=f"ล็อคไมค์โดย {i.user}")
    await i.response.send_message(f"🔒 ล็อคไมค์ {member.mention} แล้ว (จะบังคับเมื่อลงห้อง)")


@app_commands.command(name="vunmute", description="ปลดล็อคไมค์")
@app_commands.guild_only()
async def vunmute(i: discord.Interaction, member: discord.Member):
    if not can_unmute(i.user):
        return await i.response.send_message(
            f"⚠️ คุณไม่สามารถเปิดไมค์ให้ {member.mention} ได้ เนื่องจากแอดมินเป็นคนปิดไว้ "
            f"กรุณาติดต่อแอดมินเพื่อทำการปลด",
            ephemeral=True,
        )
    gd = g(i.guild_id)
    if member.id in gd["muted"]:
        gd["muted"].remove(member.id)
        save()
    if member.voice:
        await member.edit(mute=False, reason=f"ปลดไมค์โดย {i.user}")
    await i.response.send_message(f"🔓 ปลดไมค์ {member.mention} แล้ว")


# ลิงก์สตรีม ต้องเป็น Twitch หรือ YouTube เท่านั้น ถึงจะขึ้นสถานะสตรีมสีม่วง
STREAM_URL = "https://www.twitch.tv/discord"


@tasks.loop(minutes=5)
async def update_status():
    """สถานะสตรีม: X servers | Y members (อัปเดตทุก 5 นาที)"""
    servers = len(bot.guilds)
    members = sum(gd.member_count or 0 for gd in bot.guilds)
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Streaming(
            name=f"{servers} servers | {members} members",
            url=STREAM_URL,
        ),
    )


COMMANDS = [
    hide, show, lock, unlock, set_jail, bl_add, bl_remove,
    roles_group, vmute, vunmute,
]


async def on_ready():
    if not update_status.is_running():
        update_status.start()
    print(f"พร้อมใช้งาน: {bot.user}")


def make_bot():
    global bot
    bot = AdminBot(command_prefix="!", intents=intents)
    bot.event(on_voice_state_update)
    bot.event(on_message)
    bot.event(on_ready)
    return bot


# ถ้า Discord/Cloudflare บล็อก IP ของโฮสต์ (429) บอทจะรอแล้วลองใหม่เอง
# ไม่ล่ม และไม่ยิงล็อกอินรัว ๆ (การยิงถี่ ๆ ทำให้โดนบล็อกนานขึ้น)
if __name__ == "__main__":
    delay = 900  # 15 นาที
    while True:
        try:
            make_bot().run(TOKEN)
            break
        except discord.HTTPException as e:
            if e.status != 429:
                raise
            print(f"⚠️ ล็อกอินไม่ได้ (429 โดนบล็อกชั่วคราว) รอ {delay // 60} นาทีแล้วลองใหม่ ไม่ต้องกด Start ซ้ำ")
            time.sleep(delay)
            delay = min(delay * 2, 3600)

