import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

DATA_FILE = "modlog_data.json"
DEFAULT = {"admin_roles": [], "cards": {}, "summons": [], "noshow_hours": 1, "record_channel": None, "channels": {}, "noshow": {}, "panel": {},
           "status": {"name": "ดูแลเซิร์ฟเวอร์", "url": "https://www.twitch.tv/discord"}}
# cards   = {"ใบเหลือง": {"role_id": 123}}   ใบติดตัวถาวร ไม่มีหมดเวลา
# summons = [{"guild":1,"user":2,"voice":3,"channel":4,"deadline":ts}] หมายเรียกที่รอเช็กเวลา

TITLES = {"warn": "ตักเตือนสมาชิก", "summon": "ออกหมายเรียก", "punish": "ลงโทษสมาชิก", "ban": "แบนถาวร"}
# ── ปรับชื่อหมวดหมู่/ห้องที่นี่ได้ (ใช้ตอนสร้างครั้งแรก) ──
CATEGORY_NAME = "🚨 - Report Admin ⋆ 🚨"
CHANNEL_SPEC = [
    ("warn", "╭・❗❗❗⁺｡ตักเตือน"),
    ("summon", "│・📒⁺｡หมายเรียก"),
    ("record", "│・📙⁺｡ประวัติอาชญากร"),
    ("trash", "╰・📕⁺｡ขยะดิสคอร์ด"),
]
# เมนูไหน ให้ส่งห้องไหนเป็นค่าเริ่มต้น (แอดมินยังเปลี่ยนห้องได้ตอนใช้)
DEFAULT_ROUTE = {"warn": "warn", "summon": "summon", "punish": "record", "ban": "trash"}
# ห้อง log ของระบบป้องกันดิส (แยกหมวดจากระบบตักเตือน)
PROTECT_CATEGORY_NAME = "🛡️ - Protect Log ⋆ 🛡️"
PROTECT_CHANNEL_NAME = "│・🛡️⁺｡ล็อกป้องกันดิส"

EMBED_HEAD = {
    "warn": ("⚠️ บันทึกการตักเตือนสมาชิก (Official Warning Log)", 0xE67E22, "ระบบบันทึกการตักเตือน"),
    "summon": ("📞 บันทึกการออกหมายเรียกสมาชิก (Official Summon Log)", 0xE74C3C, "ระบบบันทึกหมายเรียก"),
    "punish": ("🟡 บันทึกการลงโทษสมาชิก (Official Punishment Log)", 0xF1C40F, "ระบบบันทึกการลงโทษ"),
    "ban": ("🚨 บันทึกการระงับการใช้งานถาวร (Permanent Ban Log)", 0xC0392B, "ระบบบันทึกการแบน"),
}


def load():
    d = {}
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
    for k, v in DEFAULT.items():
        d.setdefault(k, json.loads(json.dumps(v)))
    return d


def save(d):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def now():
    return datetime.now(timezone.utc)


def ts(dt):
    return int(dt.timestamp())


# ───────────────────────── ฟอร์มกรอกข้อมูล ─────────────────────────
class ActionModal(discord.ui.Modal):
    def __init__(self, v):
        super().__init__(title=TITLES[v.kind])
        self.v = v
        self.uid = discord.ui.TextInput(label="User ID ผู้ถูกดำเนินการ", min_length=15, max_length=22)
        self.add_item(self.uid)
        self.rule = self.extra = None
        if v.kind in ("warn", "punish"):
            self.rule = discord.ui.TextInput(label="ผิดกฎข้อที่ (ไม่บังคับ)", required=False, max_length=100)
            self.add_item(self.rule)
        if v.kind == "punish":
            self.extra = discord.ui.TextInput(label="ระยะเวลา Timeout (ชั่วโมง)", default="6", max_length=6)
            self.add_item(self.extra)
        if v.kind == "summon":
            self.extra = discord.ui.TextInput(label="ให้เข้าพบภายใน (นาที)", default="10", max_length=4)
            self.add_item(self.extra)
        self.note = discord.ui.TextInput(
            label="หมายเหตุ (ต้องระบุ)", style=discord.TextStyle.paragraph, max_length=300, required=True)
        self.add_item(self.note)

    async def on_submit(self, i: discord.Interaction):
        raw = self.uid.value.strip()
        if not raw.isdigit():
            return await i.response.send_message("User ID ต้องเป็นตัวเลขเท่านั้น", ephemeral=True)
        extra = None
        if self.extra:
            try:
                extra = float(self.extra.value)
                if extra <= 0:
                    raise ValueError
            except ValueError:
                return await i.response.send_message("ระยะเวลาต้องเป็นตัวเลขมากกว่า 0", ephemeral=True)
        note = self.note.value.strip()
        if not note:
            return await i.response.send_message("ต้องระบุหมายเหตุ", ephemeral=True)
        reason = ""
        rule = self.rule.value if self.rule else ""
        uid = int(raw)
        if self.v.kind == "ban":
            return await i.response.send_message(
                f"⚠️ ยืนยันแบนถาวร `{uid}` ?", ephemeral=True,
                view=ConfirmBan(self.v, uid, reason, note))
        await self.v.cog.run(i, self.v, uid, reason, rule, extra, note)


class ConfirmBan(discord.ui.View):
    def __init__(self, v, uid, reason, note):
        super().__init__(timeout=60)
        self.v, self.uid, self.reason, self.note = v, uid, reason, note

    @discord.ui.button(label="ยืนยันแบน", emoji="🔨", style=discord.ButtonStyle.danger)
    async def yes(self, i: discord.Interaction, b: discord.ui.Button):
        self.stop()
        await self.v.cog.run(i, self.v, self.uid, self.reason, "", None, self.note)

    @discord.ui.button(label="ยกเลิก", style=discord.ButtonStyle.secondary)
    async def no(self, i: discord.Interaction, b: discord.ui.Button):
        self.stop()
        await i.response.edit_message(content="ยกเลิกแล้ว", view=None)


# ───────────────────────── เมนูขั้นตอน (เห็นแค่คนกด) ─────────────────────────
class FlowView(discord.ui.View):
    def __init__(self, cog, kind):
        super().__init__(timeout=300)
        self.cog, self.kind = cog, kind
        self.card = self.channel_id = self.voice_id = None
        self.channel_id = cog.data["channels"].get(DEFAULT_ROUTE[kind])
        if kind in ("warn", "punish"):
            opts = [discord.SelectOption(label="ไม่ใส่ใบ", value="none")]
            for n, c in cog.data["cards"].items():
                opts.append(discord.SelectOption(label=n, value=n))
            self.card_select.options = opts[:25]
        else:
            self.remove_item(self.card_select)
        if kind != "summon":
            self.remove_item(self.voice_select)

    def summary(self):
        s = f"**{TITLES[self.kind]}**\n"
        if self.kind in ("warn", "punish"):
            s += f"ใบ: {self.card or 'ไม่ใส่ใบ'}\n"
        if self.kind == "summon":
            s += f"ห้องติดต่อประสานงาน: {f'<#{self.voice_id}>' if self.voice_id else 'ยังไม่เลือก'}\n"
        s += f"ห้องที่จะส่ง: {f'<#{self.channel_id}>' if self.channel_id else 'ยังไม่เลือก'}"
        return s

    @discord.ui.select(placeholder="เลือกใบ", options=[discord.SelectOption(label="-")])
    async def card_select(self, i: discord.Interaction, s: discord.ui.Select):
        self.card = None if s.values[0] == "none" else s.values[0]
        await i.response.edit_message(content=self.summary(), view=self)

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.voice, discord.ChannelType.stage_voice],
                       placeholder="เลือกห้องติดต่อประสานงาน (ห้องเสียง)")
    async def voice_select(self, i: discord.Interaction, s: discord.ui.ChannelSelect):
        self.voice_id = s.values[0].id
        await i.response.edit_message(content=self.summary(), view=self)

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder="เลือกห้องที่จะส่งข้อมูล")
    async def channel_select(self, i: discord.Interaction, s: discord.ui.ChannelSelect):
        self.channel_id = s.values[0].id
        await i.response.edit_message(content=self.summary(), view=self)

    @discord.ui.button(label="กรอกข้อมูล", emoji="📝", style=discord.ButtonStyle.danger)
    async def go(self, i: discord.Interaction, b: discord.ui.Button):
        if not self.channel_id or (self.kind == "summon" and not self.voice_id):
            return await i.response.send_message("เลือกห้องให้ครบก่อน", ephemeral=True)
        await i.response.send_modal(ActionModal(self))


# ───────────────────────── ปลดหมายเรียก ─────────────────────────
class ReleaseButton(discord.ui.DynamicItem[discord.ui.Button], template=r"modlog:release:(?P<uid>\d+)"):
    """ปุ่มใต้ข้อความหมายเรียก (ใช้ได้ต่อหลังบอทรีสตาร์ต)"""

    def __init__(self, uid: int):
        super().__init__(discord.ui.Button(label="ปลดหมายเรียก", emoji="🔓", style=discord.ButtonStyle.success,
                                           custom_id=f"modlog:release:{uid}"))
        self.uid = uid

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["uid"]))

    async def interaction_check(self, i: discord.Interaction):
        cog = i.client.get_cog("ModLog")
        ok = cog is not None and (cog.is_admin(i.user) or await i.client.is_owner(i.user))
        if not ok:
            await i.response.send_message("เฉพาะแอดมินเท่านั้น", ephemeral=True)
        return ok

    async def callback(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        text = await i.client.get_cog("ModLog").release(i.guild, self.uid, i.user, message=i.message)
        await i.followup.send(text, ephemeral=True)


class ReleaseModal(discord.ui.Modal, title="ปลดหมายเรียก"):
    uid = discord.ui.TextInput(label="User ID ผู้ถูกหมายเรียก", min_length=15, max_length=22)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, i: discord.Interaction):
        raw = self.uid.value.strip()
        if not raw.isdigit():
            return await i.response.send_message("User ID ต้องเป็นตัวเลขเท่านั้น", ephemeral=True)
        await i.response.defer(ephemeral=True)
        await i.followup.send(await self.cog.release(i.guild, int(raw), i.user), ephemeral=True)


# ───────────────────────── เมนูหลักที่ส่งค้างไว้ในแชท ─────────────────────────
class PanelView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    async def _open(self, i: discord.Interaction, kind):
        if not (self.cog.is_admin(i.user) or await self.cog.bot.is_owner(i.user)):
            return await i.response.send_message("เฉพาะแอดมินเท่านั้น", ephemeral=True)
        v = FlowView(self.cog, kind)
        await i.response.send_message(v.summary(), view=v, ephemeral=True)

    @discord.ui.button(label="ตักเตือน", emoji="⚠️", style=discord.ButtonStyle.primary, custom_id="modlog:warn")
    async def b_warn(self, i, b):
        await self._open(i, "warn")

    @discord.ui.button(label="หมายเรียก", emoji="📞", style=discord.ButtonStyle.primary, custom_id="modlog:summon")
    async def b_summon(self, i, b):
        await self._open(i, "summon")

    @discord.ui.button(label="ลงโทษ", emoji="🟡", style=discord.ButtonStyle.secondary, custom_id="modlog:punish")
    async def b_punish(self, i, b):
        await self._open(i, "punish")

    @discord.ui.button(label="แบนถาวร", emoji="🔨", style=discord.ButtonStyle.danger, custom_id="modlog:ban")
    async def b_ban(self, i, b):
        await self._open(i, "ban")

    @discord.ui.button(label="ปลดหมายเรียก", emoji="🔓", style=discord.ButtonStyle.success, custom_id="modlog:release_panel")
    async def b_release(self, i, b):
        if not (self.cog.is_admin(i.user) or await self.cog.bot.is_owner(i.user)):
            return await i.response.send_message("เฉพาะแอดมินเท่านั้น", ephemeral=True)
        await i.response.send_modal(ReleaseModal(self.cog))


# ───────────────────────── เมนูตั้งค่า (เจ้าของเท่านั้น) ─────────────────────────
class CardModal(discord.ui.Modal, title="เพิ่ม/แก้ไขใบ"):
    name = discord.ui.TextInput(label="ชื่อใบ (เช่น ใบเหลือง)", max_length=50)
    role_id = discord.ui.TextInput(label="ID ยศที่จะใส่ให้เมื่อได้รับใบนี้", max_length=22)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, i: discord.Interaction):
        try:
            rid = int(self.role_id.value)
        except ValueError:
            return await i.response.send_message("กรอกตัวเลขให้ถูกต้อง", ephemeral=True)
        if not i.guild.get_role(rid):
            return await i.response.send_message("ไม่พบยศนี้ในเซิร์ฟเวอร์", ephemeral=True)
        self.cog.data["cards"][self.name.value] = {"role_id": rid}
        save(self.cog.data)
        await i.response.send_message(f"บันทึก {self.name.value} แล้ว", ephemeral=True)


class DelCardModal(discord.ui.Modal, title="ลบใบ"):
    name = discord.ui.TextInput(label="ชื่อใบที่จะลบ", max_length=50)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, i: discord.Interaction):
        if self.cog.data["cards"].pop(self.name.value, None) is None:
            return await i.response.send_message("ไม่พบใบนี้", ephemeral=True)
        save(self.cog.data)
        await i.response.send_message("ลบแล้ว", ephemeral=True)


class NoShowModal(discord.ui.Modal, title="โทษกรณีไม่มาตามนัด"):
    hours = discord.ui.TextInput(label="Timeout กี่ชั่วโมง", max_length=6, default="1")

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, i: discord.Interaction):
        try:
            h = float(self.hours.value)
            if h <= 0:
                raise ValueError
        except ValueError:
            return await i.response.send_message("กรอกตัวเลขมากกว่า 0", ephemeral=True)
        self.cog.data["noshow_hours"] = h
        save(self.cog.data)
        await i.response.send_message(f"ตั้งเป็น {h} ชั่วโมงแล้ว", ephemeral=True)


class SetupView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=600)
        self.cog = cog

    @discord.ui.select(cls=discord.ui.RoleSelect, min_values=1, max_values=25, placeholder="➕ เพิ่มยศที่ใช้คำสั่งได้")
    async def add_roles(self, i: discord.Interaction, s: discord.ui.RoleSelect):
        self.cog.data["admin_roles"] = list(set(self.cog.data["admin_roles"]) | {r.id for r in s.values})
        save(self.cog.data)
        await i.response.edit_message(embed=self.cog.config_embed(), view=self)

    @discord.ui.select(cls=discord.ui.RoleSelect, min_values=1, max_values=25, placeholder="➖ ลบยศที่ใช้คำสั่งได้")
    async def del_roles(self, i: discord.Interaction, s: discord.ui.RoleSelect):
        self.cog.data["admin_roles"] = list(set(self.cog.data["admin_roles"]) - {r.id for r in s.values})
        save(self.cog.data)
        await i.response.edit_message(embed=self.cog.config_embed(), view=self)

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder="📕 ตั้งห้องประวัติอาชญากร (ลงเมื่อโดนใส่ใบ)")
    async def set_record(self, i: discord.Interaction, s: discord.ui.ChannelSelect):
        self.cog.data["record_channel"] = s.values[0].id
        save(self.cog.data)
        await i.response.edit_message(embed=self.cog.config_embed(), view=self)

    @discord.ui.button(label="เพิ่ม/แก้ไขใบ", emoji="🏷️", style=discord.ButtonStyle.primary)
    async def add_card(self, i, b):
        await i.response.send_modal(CardModal(self.cog))

    @discord.ui.button(label="ลบใบ", emoji="🗑️", style=discord.ButtonStyle.secondary)
    async def del_card(self, i, b):
        await i.response.send_modal(DelCardModal(self.cog))

    @discord.ui.button(label="โทษไม่มาตามนัด", emoji="⏰", style=discord.ButtonStyle.secondary)
    async def noshow(self, i, b):
        await i.response.send_modal(NoShowModal(self.cog))

    @discord.ui.button(label="รีเฟรช", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh(self, i, b):
        await i.response.edit_message(embed=self.cog.config_embed(), view=self)


# ───────────────────────── แจ้ง error ให้เห็นทันที (แทนข้อความ "ไม่ตอบสนอง") ─────────────────────────
async def report_error(i: discord.Interaction, error: Exception):
    traceback.print_exception(type(error), error, error.__traceback__)   # ไปโผล่ใน Console ของโฮสต์
    msg = f"⚠️ เกิดข้อผิดพลาด: `{type(error).__name__}: {str(error)[:300]}`"
    try:
        if i.response.is_done():
            await i.followup.send(msg, ephemeral=True)
        else:
            await i.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


async def _view_error(self, i: discord.Interaction, error: Exception, item=None):
    await report_error(i, error)


async def _modal_error(self, i: discord.Interaction, error: Exception):
    await report_error(i, error)


for _c in (PanelView, FlowView, ConfirmBan, SetupView):
    _c.on_error = _view_error
for _c in (ActionModal, ReleaseModal, CardModal, DelCardModal, NoShowModal):
    _c.on_error = _modal_error
ReleaseButton.on_error = _modal_error


# ───────────────────────── Cog หลัก ─────────────────────────
class ModLog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = load()

    async def cog_load(self):
        self.bot.add_view(PanelView(self))   # ให้ปุ่มในแชทใช้ได้ต่อหลังบอทรีสตาร์ต
        self.bot.add_dynamic_items(ReleaseButton)
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    def wl(self):
        """ไวท์ลิสต์จากระบบป้องกัน (/protect) → ถือเป็นแอดมินของระบบนี้ด้วย"""
        pc = self.bot.get_cog("Protect")
        if pc is None:
            return [], []
        return pc.cfg.get("whitelist_roles", []), pc.cfg.get("whitelist_users", [])

    def is_admin(self, member):
        roles = getattr(member, "roles", [])
        if any(r.id in self.data["admin_roles"] for r in roles):
            return True
        wl_roles, wl_users = self.wl()
        return member.id in wl_users or any(r.id in wl_roles for r in roles)

    def config_embed(self):
        roles = "\n".join(f"<@&{r}>" for r in self.data["admin_roles"]) or "ยังไม่มี"
        cards = "\n".join(f"**{n}** → <@&{c['role_id']}>"
                          for n, c in self.data["cards"].items()) or "ยังไม่มี"
        e = discord.Embed(title="⚙️ ตั้งค่าระบบ", color=0x5865F2)
        e.add_field(name="ยศที่ใช้คำสั่งได้", value=roles[:1000], inline=False)
        wl_roles, wl_users = self.wl()
        wl_txt = "\n".join([f"<@&{r}>" for r in wl_roles] + [f"<@{u}>" for u in wl_users]) or "ไม่มี"
        e.add_field(name="ใช้ได้ด้วย (ไวท์ลิสต์จาก /protect)", value=wl_txt[:1000], inline=False)
        e.add_field(name="รายการใบ", value=cards[:1000], inline=False)
        e.add_field(name="โทษกรณีไม่มาตามนัด", value=f"Timeout {self.data['noshow_hours']} ชม.", inline=False)
        rc = self.data["record_channel"]
        e.add_field(name="ห้องประวัติอาชญากร", value=f"<#{rc}>" if rc else "ยังไม่ตั้ง", inline=False)
        return e

    # ── สถานะสตรีมมิ่ง ──
    async def apply_status(self):
        st = self.data["status"]
        await self.bot.change_presence(activity=discord.Streaming(name=st["name"], url=st["url"]))

    @commands.Cog.listener()
    async def on_ready(self):
        await self.apply_status()

    @app_commands.command(name="status", description="ตั้งสถานะสตรีมมิ่งของบอท (เจ้าของบอทเท่านั้น)")
    @app_commands.describe(text="ข้อความที่จะแสดง", url="ลิงก์ Twitch หรือ YouTube (ต้องมี เพื่อให้เป็นสีม่วง)")
    async def status_cmd(self, i: discord.Interaction, text: str, url: str = None):
        if not await self.bot.is_owner(i.user):
            return await i.response.send_message("เฉพาะเจ้าของเท่านั้น", ephemeral=True)
        if url and not ("twitch.tv/" in url or "youtube.com/" in url or "youtu.be/" in url):
            return await i.response.send_message("ลิงก์ต้องเป็น Twitch หรือ YouTube", ephemeral=True)
        self.data["status"]["name"] = text[:128]
        if url:
            self.data["status"]["url"] = url
        save(self.data)
        await self.apply_status()
        await i.response.send_message("เปลี่ยนสถานะสตรีมมิ่งแล้ว", ephemeral=True)

    @app_commands.command(name="postpanel", description="ส่งเมนูค้างไว้ในแชท แนบรูปได้ (เจ้าของบอทเท่านั้น)")
    @app_commands.describe(channel="ห้องที่จะส่ง (ไม่ใส่ = ห้องนี้)", image="รูปใหญ่บนเมนู",
                           thumbnail="รูปเล็กมุมขวาบนของเมนู", title="หัวข้อเมนู",
                           description="ข้อความในเมนู (พิมพ์ \\n เพื่อขึ้นบรรทัดใหม่)",
                           clear="ล้างรูป/ข้อความที่เคยตั้งไว้ กลับเป็นค่าเริ่มต้น")
    @app_commands.guild_only()
    async def postpanel(self, i: discord.Interaction, channel: discord.TextChannel = None,
                        image: discord.Attachment = None, thumbnail: discord.Attachment = None,
                        title: str = None, description: str = None, clear: bool = False):
        if not await self.bot.is_owner(i.user):
            return await i.response.send_message("เฉพาะเจ้าของเท่านั้น", ephemeral=True)
        await i.response.defer(ephemeral=True)
        ch = channel or i.channel
        pn = self.data["panel"]
        if clear:
            for k in ("title", "description", "image", "thumb"):
                pn.pop(k, None)

        os.makedirs("panel_assets", exist_ok=True)
        for key, att in (("image", image), ("thumb", thumbnail)):
            if att is None:
                continue
            if not (att.content_type or "").startswith("image/"):
                return await i.followup.send("ไฟล์ที่แนบต้องเป็นรูปภาพ", ephemeral=True)
            if att.size > 8 * 1024 * 1024:
                return await i.followup.send("รูปใหญ่เกิน 8MB", ephemeral=True)
            ext = os.path.splitext(att.filename)[1].lower() or ".png"
            path = os.path.join("panel_assets", f"{key}{ext}")
            with open(path, "wb") as f:
                f.write(await att.read())
            pn[key] = path
        if title:
            pn["title"] = title[:256]
        if description:
            pn["description"] = description.replace("\\n", "\n")[:2000]

        e = discord.Embed(
            title=pn.get("title") or "🚨 ระบบจัดการผู้ใช้ (เฉพาะแอดมิน)",
            description=pn.get("description") or "กดปุ่มด้านล่างเพื่อเลือกดำเนินการ\n"
                        "⚠️ ตักเตือน • 📞 หมายเรียก • 🟡 ลงโทษ • 🔨 แบนถาวร • 🔓 ปลดหมายเรียก",
            color=0x5865F2)
        files = []
        for key, setter in (("image", e.set_image), ("thumb", e.set_thumbnail)):
            path = pn.get(key)
            if path and os.path.exists(path):
                fn = os.path.basename(path)
                files.append(discord.File(path, filename=fn))
                setter(url=f"attachment://{fn}")

        old = pn.get("msg")
        sent = await ch.send(embed=e, files=files, view=PanelView(self))
        if old:                                  # ลบเมนูเก่า เหลืออันเดียว
            try:
                oc = i.guild.get_channel(old[0])
                if oc:
                    await (await oc.fetch_message(old[1])).delete()
            except discord.HTTPException:
                pass
        pn["msg"] = [ch.id, sent.id]
        save(self.data)
        await i.followup.send(f"ส่งเมนูไปที่ {ch.mention} แล้ว", ephemeral=True)

    @app_commands.command(name="setup", description="ตั้งค่ายศและใบ (เจ้าของบอทเท่านั้น)")
    @app_commands.guild_only()
    async def setup_cmd(self, i: discord.Interaction):
        if not await self.bot.is_owner(i.user):
            return await i.response.send_message("เฉพาะเจ้าของเท่านั้น", ephemeral=True)
        await i.response.send_message(embed=self.config_embed(), view=SetupView(self), ephemeral=True)

    @app_commands.command(name="createchannels", description="สร้างหมวดหมู่และห้อง log แอดมินอัตโนมัติ (เจ้าของบอทเท่านั้น)")
    @app_commands.guild_only()
    async def createchannels(self, i: discord.Interaction):
        if not await self.bot.is_owner(i.user):
            return await i.response.send_message("เฉพาะเจ้าของเท่านั้น", ephemeral=True)
        await i.response.defer(ephemeral=True)
        guild = i.guild
        ow = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                  embed_links=True, read_message_history=True),
        }
        missing = True
        for rid in set(self.data["admin_roles"]) | set(self.wl()[0]):
            r = guild.get_role(rid)
            if r:
                missing = False
                ow[r] = discord.PermissionOverwrite(view_channel=True, read_message_history=True,
                                                    send_messages=False)
        try:
            ids = {}
            cat = guild.get_channel(self.data["channels"].get("category", 0))
            if cat is None:
                cat = await guild.create_category(CATEGORY_NAME, overwrites=ow, reason=f"สร้างโดย {i.user}")
            else:
                await cat.edit(overwrites=ow)
            ids["category"] = cat.id
            lines = []
            for key, name in CHANNEL_SPEC:
                ch = guild.get_channel(self.data["channels"].get(key, 0))
                if ch is None:
                    ch = await guild.create_text_channel(name, category=cat, overwrites=ow,
                                                         reason=f"สร้างโดย {i.user}")
                    lines.append(f"สร้าง {ch.mention}")
                else:
                    await ch.edit(category=cat, overwrites=ow)
                    lines.append(f"มีอยู่แล้ว {ch.mention} (อัปเดตสิทธิ์)")
                ids[key] = ch.id

            # หมวดแยกสำหรับ log ระบบป้องกันดิส (ไม่ปนกับระบบตักเตือน)
            pcat = guild.get_channel(self.data["channels"].get("protect_category", 0))
            if pcat is None:
                pcat = await guild.create_category(PROTECT_CATEGORY_NAME, overwrites=ow, reason=f"สร้างโดย {i.user}")
            else:
                await pcat.edit(overwrites=ow)
            ids["protect_category"] = pcat.id
            pch = guild.get_channel(self.data["channels"].get("protect_log", 0))
            if pch is None:
                pch = await guild.create_text_channel(PROTECT_CHANNEL_NAME, category=pcat, overwrites=ow,
                                                      reason=f"สร้างโดย {i.user}")
                plines = [f"สร้าง {pch.mention}"]
            else:
                await pch.edit(category=pcat, overwrites=ow)
                plines = [f"มีอยู่แล้ว {pch.mention} (อัปเดตสิทธิ์)"]
            ids["protect_log"] = pch.id
        except discord.Forbidden:
            return await i.followup.send("บอทไม่มีสิทธิ์ Manage Channels / Manage Roles", ephemeral=True)
        self.data["channels"] = ids
        self.data["record_channel"] = ids["record"]
        save(self.data)
        pc = self.bot.get_cog("Protect")          # ผูกเป็นห้อง log ของระบบป้องกันให้อัตโนมัติ
        if pc is not None:
            try:
                pc.cfg["log_channel"] = ids["protect_log"]
                sys.modules[type(pc).__module__].save(pc.cfg)
                plines.append("ตั้งเป็นห้อง log ของระบบป้องกันให้แล้ว")
            except Exception:
                plines.append("⚠️ ตั้งเป็นห้อง log ป้องกันให้เองไม่สำเร็จ ให้ตั้งที่ /protect")
        else:
            plines.append("ตั้งห้องนี้เป็นห้อง log ที่ /protect")
        msg = f"หมวดหมู่ **{cat.name}**\n" + "\n".join(lines)
        msg += f"\n\nหมวดหมู่ **{pcat.name}** (แยกจากระบบตักเตือน)\n" + "\n".join(plines)
        if missing:
            msg += "\n\n⚠️ ยังไม่ได้ตั้งยศแอดมินใน /setup ตอนนี้เห็นแค่บอทกับแอดมินเซิร์ฟเวอร์ ตั้งยศแล้วรันคำสั่งนี้ซ้ำเพื่ออัปเดตสิทธิ์"
        await i.followup.send(msg, ephemeral=True)

    async def release(self, guild, uid, by, message=None):
        """ปลดหมายเรียก: ยกเลิกการนับเวลา + ปลด Timeout ที่มาจากการไม่มาตามนัด + อัปเดตข้อความ"""
        d = self.data
        pend = [x for x in d["summons"] if x["guild"] == guild.id and x["user"] == uid]
        d["summons"] = [x for x in d["summons"] if not (x["guild"] == guild.id and x["user"] == uid)]
        until = d["noshow"].pop(f"{guild.id}:{uid}", None)
        try:
            member = guild.get_member(uid) or await guild.fetch_member(uid)
        except discord.HTTPException:
            member = None
        lifted = False
        if until and until > ts(now()) and member:
            try:
                await member.timeout(None, reason=f"ปลดหมายเรียกโดย {by}")
                lifted = True
            except discord.HTTPException:
                pass
        save(d)

        if not pend and not until and message is None:
            return "ไม่พบหมายเรียกที่ค้างอยู่ของคนนี้ (อาจปลดไปแล้วหรือหมดเวลา)"

        msg = message
        if msg is None and pend and pend[0].get("msg"):
            ch = guild.get_channel(pend[0]["channel"])
            if ch:
                try:
                    msg = await ch.fetch_message(pend[0]["msg"])
                except discord.HTTPException:
                    msg = None
        stamp = ts(now())
        if msg is not None and msg.embeds:
            e = msg.embeds[0]
            e.title = "🔓 ปลดหมายเรียกแล้ว (Summon Released)"
            e.color = 0x2ECC71
            e.add_field(name="🔓 ปลดโดย", value=f"{by.mention} <t:{stamp}:R>", inline=False)
            try:
                await msg.edit(embed=e, view=None)
            except discord.HTTPException:
                pass
        else:
            ch = guild.get_channel(d["channels"].get("summon", 0))
            if ch:
                try:
                    await ch.send(embed=discord.Embed(
                        title="🔓 ปลดหมายเรียกแล้ว (Summon Released)", color=0x2ECC71,
                        description=f"<@{uid}> ถูกปลดหมายเรียกโดย {by.mention}"))
                except discord.HTTPException:
                    pass
        if member:
            try:
                await member.send(embed=discord.Embed(
                    title="🔓 หมายเรียกของคุณถูกปลดแล้ว", color=0x2ECC71,
                    description=f"ปลดโดย {by.display_name}\nไม่ต้องเข้าห้องติดต่อประสานงานแล้ว"))
            except discord.HTTPException:
                pass
        out = "ปลดหมายเรียกแล้ว"
        if lifted:
            out += " และปลด Timeout จากการไม่มาตามนัดให้แล้ว"
        return out

    async def run(self, i: discord.Interaction, v: FlowView, uid, reason, rule, extra, note):
        await i.response.defer(ephemeral=True)
        kind, guild = v.kind, i.guild
        channel = guild.get_channel(v.channel_id)
        if channel is None:
            return await i.followup.send("ไม่พบห้องที่เลือก", ephemeral=True)

        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.HTTPException:
                member = None
        target = member
        if target is None:
            if kind != "ban":
                return await i.followup.send("ไม่พบสมาชิกคนนี้ในเซิร์ฟเวอร์", ephemeral=True)
            try:
                target = await self.bot.fetch_user(uid)
            except discord.HTTPException:
                return await i.followup.send("ไม่พบผู้ใช้ ID นี้", ephemeral=True)
        if member and (member.id == i.user.id or member.bot or self.is_admin(member)
                       or member.guild_permissions.administrator or member.top_role >= guild.me.top_role):
            return await i.followup.send("ดำเนินการกับคนนี้ไม่ได้ (ตัวเอง/บอท/แอดมิน/ยศสูงกว่าบอท)", ephemeral=True)

        title, color, footer = EMBED_HEAD[kind]
        t0 = now()
        card = self.data["cards"].get(v.card) if v.card else None
        deadline = None

        e = discord.Embed(title=title, color=color, timestamp=t0)
        e.set_thumbnail(url=target.display_avatar.url)
        if kind == "warn":
            e.description = f"ผู้ใช้ {target.mention} ได้รับการตักเตือนอย่างเป็นทางการ\nกรุณาตรวจสอบและปฏิบัติตามกฎของเซิร์ฟเวอร์"
        elif kind == "punish":
            e.description = f"ผู้ใช้ {target.mention} ถูกดำเนินการจำกัดสิทธิ์และลงโทษตามมาตรการของเซิร์ฟเวอร์"
        elif kind == "summon":
            deadline = ts(t0 + timedelta(minutes=extra))
            e.description = (f"ระบบได้ออกหมายเรียกสมาชิก {target.mention} เพื่อเข้าพบทีมงานผู้ดูแล\n"
                             f"กรุณาเข้าห้องติดต่อประสานงานภายใน **{extra:g} นาที**")
        else:
            e.description = (f"ผู้ใช้ {target.mention} ถูกระงับการใช้งานถาวรและขับออกจากเซิร์ฟเวอร์\n"
                             "เนื่องจากละเมิดกฎขั้นร้ายแรงของทางเซิร์ฟเวอร์")

        e.add_field(name="👤 ข้อมูลสมาชิก (Target User)",
                    value=f"Name: {target.display_name}\nUsername: @{target.name}\nUser ID: `{target.id}`", inline=False)
        if kind == "ban":
            roles = ", ".join(r.name for r in member.roles if r != guild.default_role) if member else "ไม่ทราบ"
            e.add_field(name="🛡️ บทบาทก่อนถูกแบน (Roles)", value=(roles or "ไม่มี")[:1000], inline=False)
        if card:
            e.add_field(name="🏷️ บทลงโทษที่ได้รับ", value=f"{v.card} (ติดตัวถาวร)", inline=False)
        if kind == "punish":
            e.add_field(name="⌛ ระยะเวลาจำกัดสิทธิ์ (Timeout)", value=f"{extra:g} ชั่วโมง", inline=False)
        if kind == "summon":
            e.add_field(name="🔊 ห้องติดต่อประสานงาน", value=f"<#{v.voice_id}>", inline=False)
            e.add_field(name="⏰ กำหนดเวลาเข้าพบ", value=f"<t:{deadline}:t> (<t:{deadline}:R>)", inline=False)
        if reason:
            e.add_field(name="📝 สาเหตุ", value=reason, inline=False)
        if rule:
            e.add_field(name="📜 ผิดกฎข้อที่", value=rule, inline=False)
        e.add_field(name="🗒️ หมายเหตุ", value=note, inline=False)
        e.add_field(name="📋 แอดมินผู้ขอ (Requester)" if kind == "ban" else "👮 ผู้ดำเนินการ (Executor)",
                    value=i.user.mention, inline=False)

        # 1) DM หาสมาชิกทุกเมนู (ตักเตือน / หมายเรียก / ลงโทษ+ใบ / แบน) ก่อนดำเนินการ
        try:
            await target.send(embed=e)
            dm = "✅ ส่งถึงสมาชิกสำเร็จ"
        except discord.HTTPException:
            dm = "❌ ส่ง DM ไม่สำเร็จ (สมาชิกปิดรับ DM)"
        e.add_field(name="📮 สถานะการแจ้งเตือน DM", value=dm, inline=False)
        if kind == "summon":
            e.add_field(name="⚠️ มาตรการกรณีไม่มาตามนัด",
                        value=f"หากไม่มาตามกำหนดเวลา จะถูก Timeout {self.data['noshow_hours']:g} ชั่วโมงทันที", inline=False)
        e.set_footer(text=footer)

        # 2) ส่งไปห้องที่ระบุ
        if kind == "summon":
            rv = discord.ui.View(timeout=None)
            rv.add_item(ReleaseButton(target.id))
            sent = await channel.send(content=target.mention, embed=e, view=rv)
        else:
            sent = await channel.send(content=target.mention, embed=e)

        # 3) ส่งห้องเสร็จแล้ว ค่อยดำเนินการจริง (ใส่ใบ / timeout / แบน)
        notes = [f"ส่งไปที่ {channel.mention} แล้ว"]
        try:
            if kind in ("warn", "punish") and card:
                role = guild.get_role(card["role_id"])
                await member.add_roles(role, reason=f"{v.card} โดย {i.user}")
                notes.append(f"ใส่ {v.card} แล้ว (ติดตัวถาวร)")
                # ห้องประวัติอาชญากร: ลงเฉพาะตอนโดนใส่ใบ
                rc = self.data["record_channel"]
                rec = guild.get_channel(rc) if rc else None
                if rec is None:
                    notes.append("ยังไม่ได้ตั้งห้องประวัติอาชญากร (ตั้งที่ /setup)")
                elif rec.id != channel.id:
                    await rec.send(content=target.mention, embed=e)
                    notes.append(f"บันทึกประวัติที่ {rec.mention} แล้ว")
            if kind == "punish":
                await member.timeout(timedelta(hours=min(extra, 672)), reason=f"ลงโทษโดย {i.user}: {note}")
                notes.append(f"Timeout {min(extra, 672):g} ชั่วโมงแล้ว")
            if kind == "summon":
                self.data["summons"].append({"guild": guild.id, "user": member.id, "voice": v.voice_id,
                                             "channel": channel.id, "deadline": deadline, "msg": sent.id})
                notes.append("ตั้งเวลาเช็กการเข้าพบแล้ว")
            if kind == "ban":
                await guild.ban(target, reason=f"แบนถาวรโดย {i.user}: {note}", delete_message_seconds=0)
                notes.append("แบนแล้ว")
            save(self.data)
        except (discord.Forbidden, AttributeError):
            notes.append("⚠️ ดำเนินการบางส่วนไม่สำเร็จ (ตรวจสิทธิ์บอท: Manage Roles / Moderate Members / Ban Members และให้ยศบอทอยู่สูงกว่ายศเป้าหมาย)")
        await i.followup.send("\n".join(notes), ephemeral=True)

    @tasks.loop(seconds=30)
    async def loop(self):
        t = ts(now())
        changed = False

        keep = []
        for s in self.data["summons"]:
            if s["deadline"] > t:
                keep.append(s)
                continue
            changed = True
            g = self.bot.get_guild(s["guild"])
            if not g:
                continue
            vc = g.get_channel(s["voice"])
            present = vc is not None and any(m.id == s["user"] for m in vc.members)
            if present:
                continue
            try:
                m = await g.fetch_member(s["user"])
                await m.timeout(timedelta(hours=self.data["noshow_hours"]), reason="ไม่มาตามหมายเรียก")
                self.data["noshow"][f"{g.id}:{s['user']}"] = t + int(self.data["noshow_hours"] * 3600)
                ch = g.get_channel(s["channel"])
                if ch:
                    await ch.send(f"⏰ <@{s['user']}> ไม่มาตามหมายเรียก ถูก Timeout {self.data['noshow_hours']:g} ชั่วโมงแล้ว")
            except discord.HTTPException:
                pass
        self.data["summons"] = keep

        if changed:
            save(self.data)

    @loop.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(ModLog(bot))
