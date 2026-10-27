import asyncio
import json
import os
import re
import time
import unicodedata
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

DATA_FILE = "protect_data.json"
DEFAULT = {
    "log_channel": None,
    "whitelist_users": [],
    "whitelist_roles": [],
    "lockdown": False,
    "nuke": {"level": "medium"},
    "raid": {"level": "medium"},
    "spam": {"level": "medium"},
    "bots": {"level": "medium"},
    "links": {"level": "medium", "allow": ["tenor.com", "giphy.com"]},
    # Role Guard: ไวลิสแยกจากไวท์ลิสต์ปกติ
    #   allow_roles = ยศที่ใส่ให้คนอื่นได้ (บอทไม่ถอด) / keep_roles = ยศเมมเบอร์ที่เก็บไว้ตอนถอดทุกยศ
    "roleguard": {"enabled": True, "allow_roles": [], "keep_roles": []},
}

A = discord.AuditLogAction
DESTROY_ACTIONS = {A.channel_delete, A.role_delete}
NUKE_ACTIONS = DESTROY_ACTIONS | {
    A.channel_create, A.role_create, A.ban, A.kick,
    A.webhook_create, A.webhook_delete, A.emoji_delete,
}
RAID_MODE_SECONDS = 300

# ── ระดับการป้องกัน (ใช้เหมือนกันทุกระบบ) ──
LABEL = {"low": "🟢 Low", "medium": "🟡 Medium", "high": "🔴 High", "off": "⚪ Disable"}
ORDER = ("low", "medium", "high", "off")

MODULES = {
    "nuke": {
        "name": "Anti-Nuke", "emoji": "💣", "cmd": "antinuke",
        "levels": {
            "low": "กันการลบห้อง/ยศ (เกิน 5 ครั้งใน 10 วิ → ถอดยศ + แบน)",
            "medium": "กันลบ/สร้างห้อง ยศ แบน เตะ (เกิน 4 ครั้งใน 10 วิ)",
            "high": "เข้มสุด ทุกการกระทำ (เกิน 2 ครั้งใน 10 วิ)",
            "off": "ปิดใช้งานระบบ",
        },
    },
    "raid": {
        "name": "Anti-Raid", "emoji": "🚧", "cmd": "antiraid",
        "levels": {
            "low": "ตรวจการบุก: เข้า 15 คนใน 10 วิ → เตะ",
            "medium": "ตรวจการบุก: เข้า 8 คนใน 10 วิ → เตะ",
            "high": "เข้า 5 คนใน 10 วิ → เตะ + เตะบัญชีอายุต่ำกว่า 7 วัน",
            "off": "ปิดใช้งานระบบ",
        },
    },
    "spam": {
        "name": "Anti-Spam", "emoji": "🔇", "cmd": "antispam",
        "levels": {
            "low": "8 ข้อความใน 5 วิ / แท็ก 8+ → ลบ + Timeout 5 นาที",
            "medium": "6 ข้อความใน 5 วิ / แท็ก 5+ → ลบ + Timeout 10 นาที",
            "high": "4 ข้อความใน 5 วิ / แท็ก 3+ → ลบ + Timeout 30 นาที",
            "off": "ปิดใช้งานระบบ",
        },
    },
    "bots": {
        "name": "Anti-Bot", "emoji": "🤖", "cmd": "antibot",
        "levels": {
            "low": "แจ้งเตือนใน log เมื่อมีบอทถูกเชิญ (ไม่เตะ)",
            "medium": "เตะบอทที่คนนอกไวท์ลิสต์เชิญเข้ามา",
            "high": "เตะบอท + ถอดยศและแบนคนที่เชิญ",
            "off": "ปิดใช้งานระบบ",
        },
    },
    "links": {
        "name": "Anti-Link", "emoji": "🔗", "cmd": "antilink",
        "levels": {
            "low": "ป้องกันเฉพาะลิ้งค์เชิญเซิร์ฟเวอร์",
            "medium": "ป้องกันลิ้งค์เชิญเซิร์ฟเวอร์และที่แปลกปลอม",
            "high": "ป้องกันทุกลิ้งค์",
            "off": "ปิดใช้งานระบบ",
        },
    },
}

PRESETS = {
    "nuke": {
        "low": {"limit": 5, "seconds": 10, "acts": DESTROY_ACTIONS},
        "medium": {"limit": 4, "seconds": 10, "acts": NUKE_ACTIONS},
        "high": {"limit": 2, "seconds": 10, "acts": NUKE_ACTIONS},
    },
    "raid": {
        "low": {"joins": 15, "seconds": 10, "min_age_days": 0},
        "medium": {"joins": 8, "seconds": 10, "min_age_days": 0},
        "high": {"joins": 5, "seconds": 10, "min_age_days": 7},
    },
    "spam": {
        "low": {"msgs": 8, "seconds": 5, "dupes": 6, "mentions": 8, "timeout_min": 5},
        "medium": {"msgs": 6, "seconds": 5, "dupes": 4, "mentions": 5, "timeout_min": 10},
        "high": {"msgs": 4, "seconds": 5, "dupes": 3, "mentions": 3, "timeout_min": 30},
    },
}

# ── Anti-Link ──
ZW_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")
INVITE_RE = re.compile(
    r"(?:discord\s*\.\s*gg|discord(?:app)?\s*\.\s*com\s*/\s*invite|dsc\s*\.\s*gg|invite\s*\.\s*gg|discord\s*\.\s*me)"
    r"\s*/?\s*[\w-]{2,}", re.I)
URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s<>()\[\]]+"
    r"|(?<![\w@.-])(?:[a-z0-9-]+\.)+(?:com|net|org|io|gg|me|co|xyz|top|click|link|app|dev|th|tv|ly|to|cc|info|site|"
    r"online|shop|store|live|fun|club|vip|icu|tk|ml|ga|cf|gq|zip|mov|ru|cn|in|us|uk)(?:/[^\s<>]*)?", re.I)
OFFICIAL = ("discord.com", "discordapp.com", "discordapp.net", "discord.media", "discordstatus.com",
            "steamcommunity.com", "steampowered.com")
SHORTENERS = ("bit.ly", "tinyurl.com", "cutt.ly", "is.gd", "rb.gy", "shorturl.at", "goo.gl", "ow.ly",
              "t.ly", "tiny.cc", "rebrand.ly", "bl.ink", "s.id", "v.gd", "soo.gd")
SUS_RE = re.compile(r"d[il1]s+c?[o0]r+[dc]?|n[il1]tr[o0]|ste[a4]m|g[il1]ft|airdr[o0]p|give[a4]way|robux", re.I)
IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def link_hosts(text):
    hosts = []
    for m in URL_RE.finditer(text):
        h = re.sub(r"^https?://", "", m.group(0), flags=re.I)
        h = re.split(r"[/?#:]", h)[0].lower()
        if h.startswith("www."):
            h = h[4:]
        if "@" in h:
            h = h.split("@")[-1]
        if h:
            hosts.append(h)
    return hosts


def host_in(host, domains):
    return any(host == d or host.endswith("." + d) for d in domains)


def is_suspicious(host):
    return bool(
        host.startswith("xn--") or ".xn--" in host or IP_RE.match(host)
        or host_in(host, SHORTENERS) or host.endswith((".zip", ".mov"))
        or SUS_RE.search(host)
    )


def fill(base, d):
    for k, v in base.items():
        if isinstance(v, dict):
            d[k] = fill(v, d.get(k) if isinstance(d.get(k), dict) else {})
        else:
            d.setdefault(k, json.loads(json.dumps(v)))
    return d


def load():
    d = {}
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
    d = fill(DEFAULT, d)
    for mod in MODULES:                      # กันค่าระดับเพี้ยน
        if d[mod]["level"] not in ORDER:
            d[mod]["level"] = "medium"
    return d


def save(d):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def onoff(b):
    return "✅ เปิด" if b else "❌ ปิด"


# ชื่อยศที่ถือว่าเป็น "เมมเบอร์" (ใช้เมื่อยังไม่ได้เลือกยศเมมเบอร์เอง) เทียบหลังตัดสัญลักษณ์/เว้นวรรค
def _norm(text):
    return "".join(c for c in text.lower() if unicodedata.category(c)[0] in "LMN")


MEMBER_KEYS = {_norm(n) for n in ("member", "members", "เมมเบอร์", "สมาชิก")}


# ───────────────────────── เมนู (เจ้าของเท่านั้น) ─────────────────────────
class OwnerView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=600)
        self.cog = cog

    async def interaction_check(self, i: discord.Interaction):
        ok = await self.cog.bot.is_owner(i.user) or i.user.id == i.guild.owner_id
        if not ok:
            await i.response.send_message("เฉพาะเจ้าของเท่านั้น", ephemeral=True)
        return ok


class AllowModal(discord.ui.Modal, title="โดเมนที่อนุญาต"):
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
        self.box = discord.ui.TextInput(
            label="คั่นด้วย , (ใช้กับระดับ Medium / High)", style=discord.TextStyle.paragraph,
            default=", ".join(cog.cfg["links"]["allow"]), required=False, max_length=1500)
        self.add_item(self.box)

    async def on_submit(self, i: discord.Interaction):
        out = []
        for x in re.split(r"[,\s]+", self.box.value.lower()):
            x = re.sub(r"^https?://", "", x.strip()).split("/")[0]
            if x.startswith("www."):
                x = x[4:]
            if x and x not in out:
                out.append(x)
        self.cog.cfg["links"]["allow"] = out[:100]
        save(self.cog.cfg)
        await i.response.send_message("บันทึกโดเมนที่อนุญาตแล้ว", ephemeral=True)


class LevelView(OwnerView):
    """เมนูเลือกระดับ Low / Medium / High / Disable ใช้เหมือนกันทุกระบบ"""

    def __init__(self, cog, mod):
        super().__init__(cog)
        self.mod = mod
        lv = MODULES[mod]["levels"]
        self.level.options = [
            discord.SelectOption(label=LABEL[v].split(" ", 1)[1], description=lv[v][:100], value=v,
                                 emoji="❗" if v == "off" else "➡️")
            for v in ORDER
        ]
        if mod != "links":
            self.remove_item(self.allow)

    @discord.ui.select(placeholder="[⭐] เลือกระดับการป้องกัน", options=[discord.SelectOption(label="-")])
    async def level(self, i: discord.Interaction, s: discord.ui.Select):
        self.cog.cfg[self.mod]["level"] = s.values[0]
        save(self.cog.cfg)
        await i.response.edit_message(embed=self.cog.level_embed(self.mod), view=self)

    @discord.ui.button(label="โดเมนที่อนุญาต", emoji="🌐", style=discord.ButtonStyle.secondary)
    async def allow(self, i, b):
        await i.response.send_modal(AllowModal(self.cog))

    @discord.ui.button(label="รีเฟรช", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh(self, i, b):
        await i.response.edit_message(embed=self.cog.level_embed(self.mod), view=self)


class RoleGuardView(OwnerView):
    """/antirole = ตั้งไวลิสแยก (ยศที่ใส่ให้คนอื่นได้) + ยศเมมเบอร์ที่เก็บไว้"""

    @discord.ui.select(cls=discord.ui.RoleSelect, min_values=1, max_values=25,
                       placeholder="✅ ไวลิสแยก: ยศที่ใส่ให้คนอื่นได้ (เลือกซ้ำ = ลบออก)")
    async def allow_roles(self, i: discord.Interaction, s: discord.ui.RoleSelect):
        rg = self.cog.cfg["roleguard"]
        cur = set(rg["allow_roles"])
        for r in s.values:
            cur ^= {r.id}
        rg["allow_roles"] = list(cur)
        save(self.cog.cfg)
        await i.response.edit_message(embed=self.cog.roleguard_embed(i.guild), view=self)

    @discord.ui.select(cls=discord.ui.RoleSelect, min_values=1, max_values=25,
                       placeholder="👤 ยศเมมเบอร์ที่เก็บไว้ (เลือกซ้ำ = ลบออก)")
    async def keep_roles(self, i: discord.Interaction, s: discord.ui.RoleSelect):
        rg = self.cog.cfg["roleguard"]
        cur = set(rg["keep_roles"])
        for r in s.values:
            cur ^= {r.id}
        rg["keep_roles"] = list(cur)
        save(self.cog.cfg)
        await i.response.edit_message(embed=self.cog.roleguard_embed(i.guild), view=self)

    @discord.ui.button(label="เปิด/ปิดระบบ", emoji="🔁", style=discord.ButtonStyle.danger)
    async def toggle(self, i, b):
        rg = self.cog.cfg["roleguard"]
        rg["enabled"] = not rg["enabled"]
        save(self.cog.cfg)
        await i.response.edit_message(embed=self.cog.roleguard_embed(i.guild), view=self)

    @discord.ui.button(label="รีเฟรช", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh(self, i, b):
        await i.response.edit_message(embed=self.cog.roleguard_embed(i.guild), view=self)


class HubView(OwnerView):
    """/protect = ศูนย์รวม: เปิดเมนูแต่ละระบบ + ห้อง log + ไวท์ลิสต์ + Lockdown"""

    async def _open(self, i, mod):
        await i.response.send_message(embed=self.cog.level_embed(mod), view=LevelView(self.cog, mod), ephemeral=True)

    @discord.ui.button(label="Anti-Nuke", emoji="💣", style=discord.ButtonStyle.secondary)
    async def b_nuke(self, i, b):
        await self._open(i, "nuke")

    @discord.ui.button(label="Anti-Raid", emoji="🚧", style=discord.ButtonStyle.secondary)
    async def b_raid(self, i, b):
        await self._open(i, "raid")

    @discord.ui.button(label="Anti-Spam", emoji="🔇", style=discord.ButtonStyle.secondary)
    async def b_spam(self, i, b):
        await self._open(i, "spam")

    @discord.ui.button(label="Anti-Link", emoji="🔗", style=discord.ButtonStyle.secondary)
    async def b_links(self, i, b):
        await self._open(i, "links")

    @discord.ui.button(label="Anti-Bot", emoji="🤖", style=discord.ButtonStyle.secondary)
    async def b_bots(self, i, b):
        await self._open(i, "bots")

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder="📋 ห้อง log ของระบบป้องกัน")
    async def log_ch(self, i: discord.Interaction, s: discord.ui.ChannelSelect):
        self.cog.cfg["log_channel"] = s.values[0].id
        save(self.cog.cfg)
        await i.response.edit_message(embed=self.cog.hub_embed(), view=self)

    @discord.ui.select(cls=discord.ui.RoleSelect, min_values=1, max_values=25,
                       placeholder="✅ ไวท์ลิสต์ยศ (เลือกซ้ำ = ลบออก)")
    async def wl_roles(self, i: discord.Interaction, s: discord.ui.RoleSelect):
        cur = set(self.cog.cfg["whitelist_roles"])
        for r in s.values:
            cur ^= {r.id}
        self.cog.cfg["whitelist_roles"] = list(cur)
        save(self.cog.cfg)
        await i.response.edit_message(embed=self.cog.hub_embed(), view=self)

    @discord.ui.select(cls=discord.ui.UserSelect, min_values=1, max_values=25,
                       placeholder="✅ ไวท์ลิสต์คน (เลือกซ้ำ = ลบออก)")
    async def wl_users(self, i: discord.Interaction, s: discord.ui.UserSelect):
        cur = set(self.cog.cfg["whitelist_users"])
        for u in s.values:
            cur ^= {u.id}
        self.cog.cfg["whitelist_users"] = list(cur)
        save(self.cog.cfg)
        await i.response.edit_message(embed=self.cog.hub_embed(), view=self)

    @discord.ui.button(label="Lockdown (ปิดประตูเซิร์ฟ)", emoji="🔒", style=discord.ButtonStyle.danger)
    async def b_lock(self, i, b):
        c = self.cog.cfg
        c["lockdown"] = not c["lockdown"]
        save(c)
        await self.cog.log(i.guild, "🔒 Lockdown", f"{'เปิด' if c['lockdown'] else 'ปิด'}โดย {i.user.mention}",
                           0xE74C3C if c["lockdown"] else 0x2ECC71)
        await i.response.edit_message(embed=self.cog.hub_embed(), view=self)

    @discord.ui.button(label="Role Guard", emoji="🎭", style=discord.ButtonStyle.secondary)
    async def b_role(self, i, b):
        await i.response.send_message(embed=self.cog.roleguard_embed(i.guild),
                                      view=RoleGuardView(self.cog), ephemeral=True)

    @discord.ui.button(label="รีเฟรช", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def b_ref(self, i, b):
        await i.response.edit_message(embed=self.cog.hub_embed(), view=self)


# ───────────────────────── Cog หลัก ─────────────────────────
class Protect(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cfg = load()
        self.hist = defaultdict(deque)
        self.punished = {}
        self.joins = defaultdict(deque)
        self.raid_until = {}
        self.msgs = defaultdict(deque)
        self.dupes = defaultdict(deque)
        self.cool = {}
        self.app_owner = None
        self.rg_locks = defaultdict(asyncio.Lock)  # กัน Role Guard ทำซ้อนกับคนเดียวกัน

    async def cog_load(self):
        try:
            info = await self.bot.application_info()
            self.app_owner = info.owner.id
        except discord.HTTPException:
            pass

    # ── ตัวช่วย ──
    def preset(self, mod):
        """ค่าตามระดับที่เลือก (None = ปิด)"""
        return PRESETS[mod].get(self.cfg[mod]["level"])

    def trusted(self, guild, uid):
        c = self.cfg
        if uid in (getattr(self.bot.user, "id", None), guild.owner_id, self.app_owner) or uid in c["whitelist_users"]:
            return True
        m = guild.get_member(uid)
        return bool(m and any(r.id in c["whitelist_roles"] for r in m.roles))

    def hit(self, key, window, store=None):
        dq = (store if store is not None else self.hist)[key]
        t = time.monotonic()
        dq.append(t)
        while dq and t - dq[0] > window:
            dq.popleft()
        return len(dq)

    async def log(self, guild, title, desc, color=0xE74C3C):
        cid = self.cfg["log_channel"]
        ch = guild.get_channel(cid) if cid else None
        if ch:
            e = discord.Embed(title=title, description=desc, color=color, timestamp=datetime.now(timezone.utc))
            try:
                await ch.send(embed=e)
            except discord.HTTPException:
                pass

    # ── หน้าตาเมนู ──
    def level_embed(self, mod):
        M = MODULES[mod]
        lv = self.cfg[mod]["level"]
        lines = "\n".join(f"• {LABEL[v]} = {M['levels'][v]}" for v in ("low", "medium", "high"))
        e = discord.Embed(
            title=f"📖 {M['name']} System",
            description=f"ตั้งค่าการป้องกันจากเมนูด้านล่างได้เลยครับ\n{lines}\n\n"
                        f"ระดับปัจจุบัน: **{LABEL[lv]} ({M['levels'][lv]})**",
            color=0x5865F2)
        if mod == "links":
            e.add_field(name="🌐 โดเมนที่อนุญาต (Medium/High)",
                        value=(", ".join(self.cfg["links"]["allow"]) or "-")[:1000], inline=False)
        e.set_footer(text="ข้ามเจ้าเซิร์ฟ / เจ้าของบอท / ไวท์ลิสต์ • ตั้งไวท์ลิสต์และห้อง log ที่ /protect")
        return e

    def hub_embed(self):
        c = self.cfg
        e = discord.Embed(title="🛡️ ระบบป้องกันเซิร์ฟเวอร์",
                          description="กดปุ่มด้านล่างเพื่อเลือกระดับของแต่ละระบบ", color=0x5865F2)
        for mod, M in MODULES.items():
            lv = c[mod]["level"]
            e.add_field(name=f"{M['emoji']} {M['name']}", value=f"{LABEL[lv]}\n/{M['cmd']}")
        e.add_field(name="🔒 Lockdown", value=onoff(c["lockdown"]))
        e.add_field(name="🎭 Role Guard", value=f"{onoff(c['roleguard']['enabled'])}\n/antirole")
        lc = c["log_channel"]
        e.add_field(name="📋 ห้อง log", value=f"<#{lc}>" if lc else "ยังไม่ตั้ง", inline=False)
        wr = "\n".join(f"<@&{r}>" for r in c["whitelist_roles"]) or "-"
        wu = "\n".join(f"<@{u}>" for u in c["whitelist_users"]) or "-"
        e.add_field(name="✅ ไวท์ลิสต์ยศ", value=wr[:900])
        e.add_field(name="✅ ไวท์ลิสต์คน", value=wu[:900])
        e.set_footer(text="เจ้าเซิร์ฟ / เจ้าของบอท / ตัวบอทเอง ถูกยกเว้นอัตโนมัติ • Anti-Nuke ยกเว้นบอททุกตัว")
        return e

    async def _owner_only(self, i):
        if await self.bot.is_owner(i.user) or i.user.id == i.guild.owner_id:
            return True
        await i.response.send_message("เฉพาะเจ้าของเท่านั้น", ephemeral=True)
        return False

    async def _open_level(self, i, mod):
        if await self._owner_only(i):
            await i.response.send_message(embed=self.level_embed(mod), view=LevelView(self, mod), ephemeral=True)

    # ── คำสั่ง (หน้าตาเหมือนกันทุกอัน) ──
    @app_commands.command(name="protect", description="ศูนย์รวมระบบป้องกันเซิร์ฟเวอร์ (เจ้าของเท่านั้น)")
    @app_commands.guild_only()
    async def protect(self, i: discord.Interaction):
        if await self._owner_only(i):
            await i.response.send_message(embed=self.hub_embed(), view=HubView(self), ephemeral=True)

    @app_commands.command(name="antinuke", description="ตั้งค่าระบบกันทำลายเซิร์ฟเวอร์ (เจ้าของเท่านั้น)")
    @app_commands.guild_only()
    async def antinuke(self, i: discord.Interaction):
        await self._open_level(i, "nuke")

    @app_commands.command(name="antiraid", description="ตั้งค่าระบบกันบุก (เจ้าของเท่านั้น)")
    @app_commands.guild_only()
    async def antiraid(self, i: discord.Interaction):
        await self._open_level(i, "raid")

    @app_commands.command(name="antispam", description="ตั้งค่าระบบกันสแปม (เจ้าของเท่านั้น)")
    @app_commands.guild_only()
    async def antispam(self, i: discord.Interaction):
        await self._open_level(i, "spam")

    @app_commands.command(name="antilink", description="ตั้งค่าระบบกันลิงก์ (เจ้าของเท่านั้น)")
    @app_commands.guild_only()
    async def antilink(self, i: discord.Interaction):
        await self._open_level(i, "links")

    @app_commands.command(name="antibot", description="ตั้งค่าระบบกันบอทแปลกปลอม (เจ้าของเท่านั้น)")
    @app_commands.guild_only()
    async def antibot(self, i: discord.Interaction):
        await self._open_level(i, "bots")

    # ── Role Guard: ใครใส่ยศให้คนอื่น (ที่ไม่อยู่ไวลิสแยก) → ถอดทุกยศของคนที่ได้รับ ยกเว้นเมมเบอร์ ──
    def keep_roles(self, guild):
        """ยศเมมเบอร์ที่เก็บไว้: ที่เลือกไว้ ถ้ายังไม่เลือกจะหาจากชื่อ (Member / เมมเบอร์ / สมาชิก)"""
        ids = set(self.cfg["roleguard"]["keep_roles"])
        if ids:
            return [r for r in guild.roles if r.id in ids]
        return [r for r in guild.roles if not r.is_default() and _norm(r.name) in MEMBER_KEYS]

    def roleguard_embed(self, guild):
        rg = self.cfg["roleguard"]
        e = discord.Embed(
            title="🎭 Role Guard System",
            description="ใครใส่ยศให้คนอื่น (ยศที่ **ไม่อยู่ในไวลิสแยก**) → บอทถอด **ทุกยศ** ของคนที่ได้รับ "
                        "ยกเว้นเมมเบอร์ + แจ้งใน log\n"
                        "ผู้ใส่ที่ถูกยกเว้น: เจ้าเซิร์ฟ / เจ้าของบอท / บอทตัวเอง / บอทอื่น "
                        "(ไวท์ลิสต์ปกติ **ไม่** ถูกยกเว้น)",
            color=0x5865F2)
        e.add_field(name="สถานะ", value=onoff(rg["enabled"]))
        allow = "\n".join(f"<@&{r}>" for r in rg["allow_roles"]) or "- (ยังไม่มี: ทุกยศที่มีคนใส่ให้จะโดนถอด)"
        e.add_field(name="✅ ไวลิสแยก (ใส่ให้คนอื่นได้)", value=allow[:900], inline=False)
        keep = self.keep_roles(guild)
        if rg["keep_roles"]:
            txt = " ".join(r.mention for r in keep) or "⚠️ ไม่พบยศที่เลือกไว้"
        elif keep:
            txt = " ".join(r.mention for r in keep) + "\n(ตรวจจากชื่ออัตโนมัติ)"
        else:
            txt = "⚠️ ไม่พบยศเมมเบอร์ — เลือกด้านล่าง ไม่งั้นจะถอดทุกยศหมด"
        e.add_field(name="👤 ยศเมมเบอร์ที่เก็บไว้", value=txt[:900], inline=False)
        lc = self.cfg["log_channel"]
        e.add_field(name="📋 ห้อง log", value=f"<#{lc}>" if lc else "ยังไม่ตั้ง (ตั้งที่ /protect)", inline=False)
        e.set_footer(text="ยศไวลิสแยก / ยศบอท (managed) / ยศที่สูงกว่าบอท จะไม่ถูกถอด")
        return e

    @commands.Cog.listener("on_audit_log_entry_create")
    async def roleguard_entry(self, entry: discord.AuditLogEntry):
        if entry.action != A.member_role_update:
            return
        rg = self.cfg["roleguard"]
        if not rg["enabled"]:
            return
        guild, u = entry.guild, entry.user
        if u is None or getattr(u, "bot", False):
            return
        if u.id in (getattr(self.bot.user, "id", None), guild.owner_id, self.app_owner):
            return
        if entry.target is None:
            return
        allow = set(rg["allow_roles"])
        added = []
        for r in getattr(entry.after, "roles", None) or []:
            role = guild.get_role(r.id)
            if role is not None and not role.managed and role.id not in allow:
                added.append(role)
        if not added:
            return
        member = guild.get_member(entry.target.id)
        if member is None or member.id in (guild.owner_id, getattr(self.bot.user, "id", None)):
            return
        async with self.rg_locks[member.id]:
            await self.roleguard_strip(guild, u, member, added)

    async def roleguard_strip(self, guild, executor, member, added):
        rg = self.cfg["roleguard"]
        keep_ids = {r.id for r in self.keep_roles(guild)} | set(rg["allow_roles"])
        me_top = guild.me.top_role
        keep, removed, stuck = [], [], []
        for r in member.roles:
            if r.is_default() or r.managed or r.id in keep_ids:
                keep.append(r)
            elif r >= me_top:          # ยศสูงกว่าบอท ถอดไม่ได้
                keep.append(r)
                stuck.append(r)
            else:
                removed.append(r)
        for r in added:                # ยศที่เพิ่งใส่ แต่ cache ยังไม่อัปเดต
            if r not in removed and r not in keep and r < me_top:
                removed.append(r)
        if not removed and not stuck:
            return

        ok = True
        if removed:
            try:
                await member.edit(roles=keep, reason=f"Role Guard: {executor} ใส่ยศให้ {member} (ไม่อยู่ในไวลิสแยก)")
            except discord.HTTPException:
                ok = False

        lines = [
            f"ผู้ใส่ยศ: {executor.mention} (`{executor.id}`)",
            f"ผู้ได้รับ: {member.mention} (`{member.id}`)",
            f"ยศที่ถูกใส่: {' '.join(r.mention for r in added)}",
        ]
        if removed and ok:
            lines.append(f"✅ ถอดแล้ว {len(removed)} ยศ: " + " ".join(r.mention for r in removed)[:900])
            kept = [r for r in keep if not r.is_default()]
            if kept:
                lines.append("เก็บไว้: " + " ".join(r.mention for r in kept)[:500])
        if removed and not ok:
            lines.append("❌ ถอดไม่สำเร็จ: บอทต้องมีสิทธิ์ Manage Roles และยศบอทต้องสูงกว่าเป้าหมาย")
        if stuck:
            lines.append("⚠️ ถอดไม่ได้ (ยศสูงกว่าบอท): " + " ".join(r.mention for r in stuck)[:500])
        await self.log(guild, "🎭 Role Guard", "\n".join(lines), 0xE67E22 if ok else 0xE74C3C)

    @app_commands.command(name="antirole", description="ตั้งค่าระบบป้องกันยศ (เจ้าของเท่านั้น)")
    @app_commands.guild_only()
    async def antirole(self, i: discord.Interaction):
        if await self._owner_only(i):
            await i.response.send_message(embed=self.roleguard_embed(i.guild),
                                          view=RoleGuardView(self), ephemeral=True)

    # ── Anti-Link ──
    def link_violation(self, content):
        c = self.cfg["links"]
        if c["level"] == "off":
            return None
        text = ZW_RE.sub("", content or "")
        if INVITE_RE.search(text):
            return "ลิงก์เชิญเซิร์ฟเวอร์"
        if c["level"] == "low":
            return None
        safe = OFFICIAL + tuple(c["allow"])
        for h in link_hosts(text):
            if host_in(h, safe):
                continue
            if c["level"] == "high":
                return f"ลิงก์ ({h})"
            if is_suspicious(h):
                return f"ลิงก์น่าสงสัย ({h})"
        return None

    async def check_links(self, m: discord.Message):
        why = self.link_violation(m.content)
        if not why:
            return False
        try:
            await m.delete()
            res, ok = "ลบข้อความแล้ว", True
        except discord.NotFound:
            res, ok = "ลบข้อความแล้ว", True
        except discord.Forbidden:
            res, ok = "⚠️ ลบไม่ได้: บอทไม่มีสิทธิ์ Manage Messages ในห้องนี้", False
        except discord.HTTPException:
            res, ok = "⚠️ ลบไม่สำเร็จ", False
        await self.log(m.guild, "🔗 Anti-Link",
                       f"{m.author.mention} ใน {m.channel.mention}\nสาเหตุ: {why}\nผล: {res}", 0xE67E22)
        if ok:
            try:
                await m.channel.send(f"{m.author.mention} ห้ามส่งลิงก์ประเภทนี้ในเซิร์ฟเวอร์", delete_after=6)
            except discord.HTTPException:
                pass
        return True

    # ── Anti-Nuke / Anti-Bot (อ่านจาก Audit Log แบบเรียลไทม์) ──
    async def punish(self, guild, uid, why):
        t = time.monotonic()
        if t - self.punished.get(uid, -999) < 60:
            return
        self.punished[uid] = t
        res = []
        member = guild.get_member(uid)
        if member:
            keep = [r for r in member.roles if not r.is_default() and (r.managed or r >= guild.me.top_role)]
            try:
                await member.edit(roles=keep, reason="Anti-Nuke")
                res.append("ถอดยศทั้งหมดแล้ว")
            except discord.HTTPException:
                res.append("ถอดยศไม่สำเร็จ")
        try:
            await guild.ban(discord.Object(uid), reason=f"Anti-Nuke: {why}", delete_message_seconds=0)
            res.append("แบนแล้ว")
        except discord.HTTPException:
            res.append("แบนไม่สำเร็จ (ยศบอทต้องสูงกว่าเป้าหมาย)")
        await self.log(guild, "🛡️ Anti-Nuke", f"<@{uid}> (`{uid}`)\nสาเหตุ: {why}\nผล: {', '.join(res)}")

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        guild, u, a = entry.guild, entry.user, entry.action
        if u is None or self.trusted(guild, u.id):
            return
        if getattr(u, "bot", False):   # Anti-Nuke / Anti-Bot ยกเว้นบอททุกตัว
            return

        lvl = self.cfg["bots"]["level"]
        if a == A.bot_add and lvl != "off" and entry.target is not None:
            if lvl == "low":
                res = "แจ้งเตือนเท่านั้น (ระดับ Low)"
            else:
                try:
                    await guild.kick(discord.Object(entry.target.id), reason="Anti-Bot: บอทไม่ได้รับอนุญาต")
                    res = "เตะบอทออกแล้ว"
                except discord.HTTPException:
                    res = "เตะบอทไม่สำเร็จ (ตรวจสิทธิ์/ยศบอท)"
            await self.log(guild, "🤖 Anti-Bot",
                           f"ผู้เชิญ: <@{u.id}>\nบอท: <@{entry.target.id}> (`{entry.target.id}`)\nผล: {res}")
            if lvl == "high":
                await self.punish(guild, u.id, "เชิญบอทโดยไม่ได้รับอนุญาต")
            return

        n = self.preset("nuke")
        if not n:
            return
        if a == A.member_prune:
            return await self.punish(guild, u.id, "สั่ง Prune สมาชิก")
        if a in n["acts"]:
            cnt = self.hit(("nuke", guild.id, u.id), n["seconds"])
            if cnt >= n["limit"]:
                await self.punish(guild, u.id, f"ทำรายการผิดปกติ {cnt} ครั้งใน {n['seconds']} วิ (ล่าสุด: {a.name})")

    # ── Anti-Raid / Lockdown ──
    async def _kick(self, member, why):
        try:
            await member.guild.kick(member, reason=why)
            return True
        except discord.HTTPException:
            return False

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot or self.trusted(member.guild, member.id):
            return
        g, c = member.guild, self.cfg
        if c["lockdown"]:
            if await self._kick(member, "Lockdown"):
                await self.log(g, "🔒 Lockdown", f"เตะ {member.mention} (`{member.id}`) ที่พยายามเข้ามา", 0xE67E22)
            return
        r = self.preset("raid")
        if not r:
            return
        age = (datetime.now(timezone.utc) - member.created_at).days
        if r["min_age_days"] and age < r["min_age_days"]:
            if await self._kick(member, "บัญชีใหม่เกินไป"):
                await self.log(g, "🚧 บัญชีใหม่", f"เตะ {member.mention} อายุบัญชี {age} วัน (ขั้นต่ำ {r['min_age_days']})", 0xE67E22)
            return
        t = time.monotonic()
        if t < self.raid_until.get(g.id, 0):
            await self._kick(member, "Raid mode")
            return
        dq = self.joins[g.id]
        dq.append((t, member.id))
        while dq and t - dq[0][0] > r["seconds"]:
            dq.popleft()
        if len(dq) >= r["joins"]:
            self.raid_until[g.id] = t + RAID_MODE_SECONDS
            ids = [uid for _, uid in dq]
            dq.clear()
            kicked = 0
            for uid in ids:
                m = g.get_member(uid)
                if m and not self.trusted(g, uid) and await self._kick(m, "Anti-Raid"):
                    kicked += 1
            await self.log(g, "🚧 ตรวจพบการบุก (Raid)",
                           f"มีคนเข้า {len(ids)} คนใน {r['seconds']} วิ\nเตะแล้ว {kicked} คน\n"
                           f"โหมดบุกทำงาน {RAID_MODE_SECONDS // 60} นาที (คนใหม่ที่เข้ามาจะถูกเตะ)")

    # ── Anti-Spam ──
    async def _spam_hit(self, m: discord.Message, why, mins):
        key = (m.guild.id, m.author.id)
        t = time.monotonic()
        if t - self.cool.get(key, -999) < 10:
            return
        self.cool[key] = t
        self.msgs.pop(key, None)
        self.dupes.pop(key, None)
        res = []
        try:
            await m.channel.purge(limit=30, check=lambda x: x.author.id == m.author.id
                                  and (datetime.now(timezone.utc) - x.created_at).total_seconds() < 90)
            res.append("ลบข้อความแล้ว")
        except discord.HTTPException:
            res.append("ลบข้อความไม่สำเร็จ (ตรวจสิทธิ์ Manage Messages)")
        try:
            await m.author.timeout(timedelta(minutes=mins), reason=f"Anti-Spam: {why}")
            res.append(f"Timeout {mins} นาที")
        except discord.HTTPException:
            res.append("Timeout ไม่สำเร็จ")
        await self.log(m.guild, "🔇 Anti-Spam", f"{m.author.mention} ใน {m.channel.mention}\nสาเหตุ: {why}\nผล: {', '.join(res)}", 0xE67E22)

    def exempt(self, m: discord.Message):
        p = m.author.guild_permissions
        return p.administrator or p.manage_messages or self.trusted(m.guild, m.author.id)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        # กันแก้ข้อความทีหลังให้เป็นลิงก์
        if after.author.bot or after.guild is None or not isinstance(after.author, discord.Member):
            return
        if before.content == after.content or self.exempt(after):
            return
        await self.check_links(after)

    @commands.Cog.listener()
    async def on_message(self, m: discord.Message):
        if m.author.bot or m.guild is None or not isinstance(m.author, discord.Member):
            return
        if self.exempt(m):
            return

        if await self.check_links(m):
            return

        s = self.preset("spam")
        if not s:
            return
        key = (m.guild.id, m.author.id)

        mentions = len({u.id for u in m.mentions}) + len({r.id for r in m.role_mentions})
        if mentions >= s["mentions"]:
            return await self._spam_hit(m, f"แท็กรัว ({mentions} รายการ)", s["timeout_min"])

        if self.hit(key, s["seconds"], self.msgs) >= s["msgs"]:
            return await self._spam_hit(m, f"ส่งข้อความรัว ({s['msgs']}+ ข้อความใน {s['seconds']} วิ)", s["timeout_min"])

        if m.content:
            dq = self.dupes[key]
            t = time.monotonic()
            dq.append((t, m.content))
            while dq and t - dq[0][0] > 20:
                dq.popleft()
            if sum(1 for _, x in dq if x == m.content) >= s["dupes"]:
                await self._spam_hit(m, "ส่งข้อความซ้ำ ๆ", s["timeout_min"])


async def setup(bot: commands.Bot):
    await bot.add_cog(Protect(bot))
