"""NZ Pokémon restock bot: discord.py + SQLite, polls Shopify collection feeds."""
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import sites

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = os.getenv("GUILD_ID", "")  # optional: makes slash commands appear instantly
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "240"))
DB_PATH = os.getenv("DB_PATH", "restock.db")

# ------------------------------------------------------------------------------
# WHAT COUNTS AS A TRADING CARD PRODUCT (edit these two lists to taste)
# A product only triggers an alert if its name contains at least one INCLUDE
# word/phrase and none of the EXCLUDE ones. Capital letters don't matter, and
# plurals (booster / boosters) are handled for you. After editing: save the
# file, stop the bot with Ctrl+C, and start it again.
INCLUDE_WORDS = [
    "trading card", "tcg", "booster", "elite trainer", "etb", "blister", "tin",
    "build & battle", "build and battle", "premium collection",
    "illustration collection", "special collection", "ex box",
    "battle deck", "starter deck", "theme deck",
]
EXCLUDE_WORDS = [
    "sleeve", "deck box", "toploader", "top loader", "playmat", "play mat",
    "portfolio", "album", "pocket", "pages", "ultra pro", "dragon shield",
    "ultimate guard", "gamegenic", "plush", "figure", "figurine", "lunch",
    "pyjama", "shirt", "single", "graded", "psa", "bgs", "cgc", "capsule",
    "acrylic", "magnet", "dice", "blind box", "tech sticker",
]
# ------------------------------------------------------------------------------


def _word_list(words: list) -> "re.Pattern":
    """Whole-word match (so 'tin' won't match 'Tinkaton'), allowing a plural 's'."""
    body = "|".join(re.escape(w) for w in words)
    return re.compile(r"(?<![a-z0-9])(?:" + body + r")s?(?![a-z0-9])", re.I)


INCLUDE = _word_list(INCLUDE_WORDS)
POKEMON = re.compile(r"pok[eé]mon", re.I)  # used by stores flagged 'Pokémon only'
EXCLUDE = _word_list(EXCLUDE_WORDS)

# Starter stores, copied from your old config. Collection slugs are unverified:
# watch the console for warnings and fix them with /removestore + /addstore.
SEED_STORES = [
    ("The Game Tree", "https://thegametree.co.nz", "pokemon-products"),
    ("TCG Collector NZ", "https://tcgcollectornz.com", "pokemon-tcg-collector-nz"),
    ("Card Masters", "https://cardmasters.co.nz", "pokemon-booster-boxes-etbs"),
    ("WP Games", "https://wpgames.co.nz", "latest-pokemon-sets"),
    ("Animal Kingdoms", "https://animalkingdoms.co.nz", "pokemon-tcg"),
    ("Bay Dragon", "https://www.baydragon.co.nz", "pokemon"),
    ("Otaku Mart", "https://otakumart.co.nz", "pokemon-tcg"),
    ("TCG NZ", "https://www.tcgnz.co.nz", "pokemon"),
    ("Card Topia", "https://www.cardtopia.co.nz", "pokemon"),
]

# Stores that need a real browser window (they block plain scripts).
# The page address is kept in the "url" column. Add more with /addpage.
BROWSER_STORES = [
         ("The Warehouse", "warehouse", "https://www.thewarehouse.co.nz/search?q=pokemon"),
         ("Whitcoulls", "whitcoulls", "https://www.whitcoulls.co.nz/range/pokemon"),
]
PARSERS = {"kmart": sites.parse_kmart, "warehouse": sites.parse_warehouse,
           "whitcoulls": sites.parse_whitcoulls, "farmers": sites.parse_farmers,
           "hobbylords": sites.parse_hobbylords}
browser_pool = sites.BrowserPool()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "en-NZ,en;q=0.9",
}

LOG_PATH = os.getenv("LOG_PATH", "restock.log")  # saved next to bot.py; ~1 MB x 4 files kept
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(),
              RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")])
log = logging.getLogger("restock")

# ---------------------------------------------------------------- database
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row


def init_db() -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS stores (
            name TEXT PRIMARY KEY, url TEXT, collection TEXT, baselined INTEGER DEFAULT 0,
            kind TEXT DEFAULT 'shopify', pokemon_only INTEGER DEFAULT 0, paused INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sightings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, user_name TEXT,
            store TEXT, product TEXT, quantity TEXT, notes TEXT);
        CREATE TABLE IF NOT EXISTS products (
            store TEXT, product_id INTEGER, title TEXT, in_stock INTEGER,
            PRIMARY KEY (store, product_id));
    """)
    if not db.execute("SELECT 1 FROM stores LIMIT 1").fetchone():
        db.executemany("INSERT INTO stores (name, url, collection) VALUES (?,?,?)", SEED_STORES)
    cols = [r["name"] for r in db.execute("PRAGMA table_info(stores)")]
    if "kind" not in cols:
        db.execute("ALTER TABLE stores ADD COLUMN kind TEXT DEFAULT 'shopify'")  # older database
    if "pokemon_only" not in cols:
        db.execute("ALTER TABLE stores ADD COLUMN pokemon_only INTEGER DEFAULT 0")
    if "paused" not in cols:
        db.execute("ALTER TABLE stores ADD COLUMN paused INTEGER DEFAULT 0")
    if not get_setting("browser_seeded"):  # add Kmart/Warehouse once; /removestore sticks
        db.executemany("INSERT OR IGNORE INTO stores (name, url, collection, kind) VALUES (?,?,'',?)",
                       [(n, u, k) for n, k, u in BROWSER_STORES])
        db.execute("INSERT INTO settings VALUES ('browser_seeded','1') "
                   "ON CONFLICT(key) DO UPDATE SET value='1'")
    db.commit()


def get_setting(key: str) -> Optional[str]:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: Optional[str]) -> None:
    if value is None:
        db.execute("DELETE FROM settings WHERE key=?", (key,))
    else:
        db.execute("INSERT INTO settings VALUES (?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    db.commit()


STARTED = time.time()
store_status: dict = {}  # store name -> how its most recent check went (shown by /status)
alerts_sent = 0


def _mark(name: str, ok: bool, detail: str = "", count: int = 0) -> None:
    st = store_status.setdefault(name, {"ok_at": None, "error": None, "error_at": None, "count": 0})
    if ok:
        st.update(ok_at=time.time(), error=None, count=count)
    else:
        st.update(error=detail, error_at=time.time())


def status_lines() -> tuple:
    """One line per store for /status. Returns (lines, everything_ok)."""
    lines, all_ok = [], True
    for r in db.execute("SELECT name, paused FROM stores ORDER BY name"):
        name = r["name"]
        if r["paused"]:
            lines.append(f"⏸️ **{name}**: paused (use /resume to switch it back on)")
            continue
        st = store_status.get(name)
        last_ok = f"<t:{int(st['ok_at'])}:R>" if st and st["ok_at"] else "never"
        rest = _blocked_until.get(name, 0) - time.monotonic()
        if rest > 0:
            all_ok = False
            lines.append(f"⏳ **{name}**: resting after a block, retrying "
                         f"<t:{int(time.time() + rest)}:R> (last good check: {last_ok})")
        elif not st:
            lines.append(f"⏳ **{name}**: waiting for its first check")
        elif st["error"]:
            all_ok = False
            lines.append(f"⚠️ **{name}**: {st['error']} <t:{int(st['error_at'])}:R> "
                         f"(last good check: {last_ok})")
        else:
            lines.append(f"✅ **{name}**: {st['count']} products, checked <t:{int(st['ok_at'])}:R>")
    return lines, all_ok


# ---------------------------------------------------------------- checking
def is_trading_card(title: str) -> bool:
    return bool(INCLUDE.search(title)) and not EXCLUDE.search(title)


async def fetch_shopify(session: aiohttp.ClientSession, store: dict) -> Optional[list]:
    """Return a list of products, or None if the fetch failed (state is then left alone)."""
    url = f"{store['url'].rstrip('/')}/collections/{store['collection']}/products.json"
    items = []
    for page in range(1, 5):
        try:
            async with session.get(url, params={"limit": 250, "page": page}) as r:
                if r.status != 200:
                    log.warning("%s: HTTP %s", store["name"], r.status)
                    _mark(store["name"], False, f"HTTP {r.status}")
                    return None
                batch = (await r.json(content_type=None)).get("products", [])
            for p in batch:
                variants = p.get("variants", [])
                prices = [float(v["price"]) for v in variants if v.get("price")]
                images = p.get("images") or []
                items.append({
                    "id": p["id"],
                    "title": p["title"],
                    "link": f"{store['url'].rstrip('/')}/products/{p['handle']}",
                    "in_stock": any(v.get("available") for v in variants),
                    "price": min(prices) if prices else None,
                    "image": images[0]["src"] if images else None,
                })
        except Exception as e:
            log.warning("%s: fetch failed (%s)", store["name"], e)
            _mark(store["name"], False, "couldn't connect")
            return None
        if len(batch) < 250:
            break
    _mark(store["name"], True, count=len(items))
    return items


def record(store_name: str, items: list) -> list:
    """Compare against saved state, save the new state, return products to alert on."""
    if not items:
        return []  # nothing read: don't mark the store as baselined
    row = db.execute("SELECT baselined, pokemon_only FROM stores WHERE name=?",
                     (store_name,)).fetchone()
    baselined = bool(row and row["baselined"])
    pokemon_only = bool(row and row["pokemon_only"])
    old = {r["product_id"]: bool(r["in_stock"]) for r in db.execute(
        "SELECT product_id, in_stock FROM products WHERE store=?", (store_name,))}
    alerts = []
    for it in items:
        # Alert on: now in stock AND (brand new, or was out of stock last poll)
        if baselined and it["in_stock"] and not old.get(it["id"]) and is_trading_card(it["title"]) \
                and (not pokemon_only or POKEMON.search(it["title"])):
            alerts.append(it)
    db.executemany(
        "INSERT INTO products VALUES (?,?,?,?) ON CONFLICT(store, product_id) "
        "DO UPDATE SET title=excluded.title, in_stock=excluded.in_stock",
        [(store_name, it["id"], it["title"], int(it["in_stock"])) for it in items])
    db.execute("UPDATE stores SET baselined=1 WHERE name=?", (store_name,))
    db.commit()
    return alerts  # first successful poll of a store is a silent baseline


_blocked_until: dict = {}  # store name -> time.monotonic() when we may try again
kmart_api_lock = asyncio.Lock()
_block_count: dict = {}


@tasks.loop(minutes=10)
async def poll_kmart():
    log.info("Running scheduled Kmart check (10 min)...")
    row = db.execute("SELECT * FROM stores WHERE name = 'Kmart NZ'").fetchone()
    if row:
        store = dict(row)
        items = await fetch_kmart_curl(store)
        if items:
            alerts = record(store["name"], items)
            if alerts:
                await send_alerts(alerts)
                
                
@tasks.loop(minutes=43)
async def poll_farmers():
    log.info("Running scheduled Farmers check (43 min)...")
    row = db.execute("SELECT * FROM stores WHERE name = 'Farmers'").fetchone()
    if row:
        store = dict(row)
        items = await fetch_farmers(store)
        if items:
            log.info(f"--- FARMERS DEBUG: Found {len(items)} items! ---")
            alerts = record(store["name"], items)
            if alerts:
                await send_alerts(alerts)
async def fetch_kmart_curl(store: dict) -> Optional[list]:
    if _blocked_until.get(store["name"], 0) > time.monotonic():
        return None
    name = store["name"]
    try:
        items = await sites.get_kmart_items_scrapingant(store["url"])
    except sites.BlockedError as e:
        n = _block_count[name] = _block_count.get(name, 0) + 1
        minutes = min(15 * 2 ** (n - 1), 60)
        _blocked_until[name] = time.monotonic() + minutes * 60
        log.warning("%s: blocked by the site (%s). Resting %d min before trying again", name, e, minutes)
        _mark(name, False, f"blocked by the site ({e})")
        return None
    except Exception as e:       
        _mark(name, False, str(e)[:100] or "check failed")
        log.warning("%s: fetch failed (%s)", name, e)
        return None

    if not items:
        log.warning("%s: page loaded but no products found", name)
        _mark(name, False, "page loaded but no products found")
        return None

    _block_count.pop(name, None)
    _mark(name, True, count=len(items))
    log.info(f"--- KMART DEBUG: Found {len(items)} items! ---")    
    return items

    
async def fetch_farmers(store: dict) -> Optional[list]:
    if _blocked_until.get(store["name"], 0) > time.monotonic():
        return None
    name = store["name"]
    try:
        items = await sites.get_farmers_items_browser(store["url"])
    except sites.BlockedError as e:
        n = _block_count[name] = _block_count.get(name, 0) + 1
        minutes = min(15 * 2 ** (n - 1), 60)
        _blocked_until[name] = time.monotonic() + minutes * 60
        log.warning("%s: blocked by the site (%s). Resting %d min before trying again", name, e, minutes)
        _mark(name, False, f"blocked by the site ({e})")
        return None
    except Exception as e:
        _mark(name, False, str(e)[:100] or "check failed")
        log.warning("%s: fetch failed (%s)", name, e)
        return None
    if not items:
        log.warning("%s: page loaded but no products found", name)
        _mark(name, False, "page loaded but no products found")
        return None
    _block_count.pop(name, None)
    _mark(name, True, count=len(items))
    return items
    
async def fetch_browser(store: dict) -> Optional[list]:
    """Kmart / The Warehouse via the real browser window. None = failed, state untouched.
    If a site blocks us we back off (15, 30, then 60 minutes) instead of hammering it."""
    name = store["name"]
    wait = _blocked_until.get(name, 0) - time.monotonic()
    if wait > 0:
        log.debug("%s: resting after a block, next try in about %d min", name, wait // 60 + 1)
        return None
    try:
        if store["kind"] == "warehouse":
            items = await browser_pool.warehouse_items(store["url"])
        elif store["kind"] == "hobbylords":
            items = await sites.get_hobbylords_items_browser(store["url"])
        else:
            items = await browser_pool.get_items_scrolling(store["url"], PARSERS[store["kind"]])
    except sites.BlockedError as e:
        n = _block_count[name] = _block_count.get(name, 0) + 1
        minutes = min(15 * 2 ** (n - 1), 60)
        _blocked_until[name] = time.monotonic() + minutes * 60
        log.warning("%s: blocked by the site (%s). Resting %d min before trying again",
                    name, e, minutes)
        _mark(name, False, f"blocked by the site ({e})")
        return None
    except Exception as e:
        _mark(name, False, str(e)[:100] or "check failed")
        log.warning("%s: browser check failed (%s)", name, e)
        return None
    if not items:
        log.warning("%s: page loaded but no products found (layout changed?)", name)
        _mark(name, False, "page loaded but no products found")
        return None
    _block_count.pop(name, None)  # a good check resets the back-off
    _mark(name, True, count=len(items))
    return items


async def check_store(session, store: dict) -> list:
    if store["kind"] == "shopify":
        items = await fetch_shopify(session, store)
    elif store["kind"] == "kmart_curl":
            items = await fetch_kmart_curl(store)
    else:
        items = await fetch_browser(store)
    if items is None:
        return []
    return record(store["name"], items)


async def run_check(kind: str = "shopify") -> int:
    rows = [dict(r) for r in db.execute("SELECT * FROM stores WHERE COALESCE(paused, 0) = 0")]
    if kind == "shopify":
        stores = [s for s in rows if s["kind"] == "shopify"]
        async with aiohttp.ClientSession(headers=HEADERS,
                                         timeout=aiohttp.ClientTimeout(total=20)) as session:
            results = await asyncio.gather(*(check_store(session, s) for s in stores))
    else:
        stores = [s for s in rows if s["kind"] != "shopify"]
        results = [await check_store(None, s) for s in stores]  # one page at a time
    alerts = [(s, it) for s, res in zip(stores, results) for it in res]
    if alerts:
        await send_alerts(alerts)
    return len(alerts)


# ---------------------------------------------------------------- discord
intents = discord.Intents.default()


class RestockBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        if GUILD_ID:
            guild = discord.Object(int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        poll.start()
        poll_browser.start()

    async def close(self) -> None:
        await browser_pool.close()
        await super().close()


bot = RestockBot()
admin = app_commands.default_permissions(manage_guild=True)


def build_embed(store: dict, it: dict) -> discord.Embed:
    e = discord.Embed(
        title=it["title"][:256],
        url=it["link"],
        colour=discord.Colour.green(),
        timestamp=datetime.now(timezone.utc),  # Discord shows this in each viewer's local time
    )
    e.set_author(name=store["name"])
    if it["price"] is not None:
        e.add_field(name="Price", value=f"${it['price']:.2f}")
    if it["image"]:
        e.set_thumbnail(url=it["image"])
    return e


async def send_alerts(alerts: list) -> None:
    global alerts_sent
    channel_id = get_setting("channel_id")
    if not channel_id:
        log.warning("%d alert(s) but no channel set. Run /setchannel", len(alerts))
        return
    channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
    role_id = get_setting("role_id")
    ping = f"<@&{role_id}> " if role_id else ""
    for i in range(0, len(alerts), 10):  # Discord allows 10 embeds per message
        chunk = alerts[i:i + 10]
        for s, it in chunk:
            log.info("ALERT %s: %s %s", s["name"], it["title"], it["link"])
        await channel.send(
            content=f"{ping}🎴 **Restock!**",
            embeds=[build_embed(s, it) for s, it in chunk],
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
        alerts_sent += len(chunk)
        log.info("Sent %d alert(s)", len(chunk))


@tasks.loop(seconds=POLL_SECONDS)
async def poll() -> None:
    try:
        await run_check()
    except Exception:
        log.exception("Poll failed")  # never let one bad poll kill the loop


@poll.before_loop
async def _wait_ready() -> None:
    await bot.wait_until_ready()


@tasks.loop(minutes=3)
async def poll_browser() -> None:
    try:
        await run_check("browser")
    except Exception:
        log.exception("Browser poll failed")


@poll_browser.before_loop
async def _wait_ready_browser() -> None:
    await bot.wait_until_ready()


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s, polling every %ss", bot.user, POLL_SECONDS)
    if not poll_kmart.is_running():
        poll_kmart.start()
    if not poll_farmers.is_running():
        poll_farmers.start()


# ---------------------------------------------------------------- commands
@bot.tree.command(description="Post restock alerts in this channel")
@admin
async def setchannel(interaction: discord.Interaction) -> None:
    set_setting("channel_id", str(interaction.channel_id))
    await interaction.response.send_message("✅ Restock alerts will post in this channel.")


@bot.tree.command(description="Role to ping on restocks (leave empty to turn pings off)")
@admin
async def setrole(interaction: discord.Interaction, role: Optional[discord.Role] = None) -> None:
    set_setting("role_id", str(role.id) if role else None)
    msg = f"✅ Will ping {role.mention}" if role else "✅ Pings turned off."
    await interaction.response.send_message(msg, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(description="Add a Shopify store (e.g. url https://shop.co.nz, collection pokemon)")
@admin
async def addstore(interaction: discord.Interaction, name: str, url: str, collection: str,
                   pokemon_only: bool = False) -> None:
    if not url.startswith("http"):
        await interaction.response.send_message("❌ URL must start with https://")
        return
    await interaction.response.defer()
    store = {"name": name, "url": url.rstrip("/"), "collection": collection.strip("/")}
    try:
        db.execute("INSERT INTO stores (name, url, collection, pokemon_only) VALUES (?,?,?,?)",
                   (store["name"], store["url"], store["collection"], int(pokemon_only)))
        db.commit()
    except sqlite3.IntegrityError:
        await interaction.followup.send(f"❌ A store named **{name}** already exists.")
        return
    async with aiohttp.ClientSession(headers=HEADERS,
                                     timeout=aiohttp.ClientTimeout(total=20)) as session:
        items = await fetch_shopify(session, store)
    if items is None:
        db.execute("DELETE FROM stores WHERE name=?", (name,))
        db.commit()
        await interaction.followup.send("❌ Couldn't read that collection. Check the URL and slug.")
        return
    record(name, items)  # silent baseline
    await interaction.followup.send(f"✅ Added **{name}**, tracking {len(items)} products.")


@bot.tree.command(description="Add a Kmart NZ, Warehouse or Whitcoulls page (paste the page address)")
@app_commands.choices(site=[app_commands.Choice(name="Kmart NZ", value="kmart"),
                            app_commands.Choice(name="The Warehouse", value="warehouse"),
                            app_commands.Choice(name="Whitcoulls", value="whitcoulls")])
@admin
async def addpage(interaction: discord.Interaction, site: app_commands.Choice[str],
                  name: str, url: str, pokemon_only: bool = False) -> None:
    domain = {"kmart": "kmart.co.nz", "warehouse": "thewarehouse.co.nz",
              "whitcoulls": "whitcoulls.co.nz"}[site.value]
    if not url.startswith("http") or domain not in url:
        await interaction.response.send_message(f"❌ That address needs to be a {domain} page.")
        return
    await interaction.response.defer()
    try:
        db.execute("INSERT INTO stores (name, url, collection, kind, pokemon_only) VALUES (?,?,'',?,?)",
                   (name, url, site.value, int(pokemon_only)))
        db.commit()
    except sqlite3.IntegrityError:
        await interaction.followup.send(f"❌ A store named **{name}** already exists.")
        return
    items = await fetch_browser({"name": name, "url": url, "kind": site.value})
    if items is None:
        db.execute("DELETE FROM stores WHERE name=?", (name,))
        db.commit()
        await interaction.followup.send("❌ Couldn't read that page. Check the address and the console.")
        return
    record(name, items)  # silent baseline
    await interaction.followup.send(f"✅ Added **{name}**, tracking {len(items)} products.")


@bot.tree.command(description="Only alert on products with 'Pokémon' in the name (for mixed card stores)")
@admin
async def pokemononly(interaction: discord.Interaction, name: str, enabled: bool = True) -> None:
    cur = db.execute("UPDATE stores SET pokemon_only=? WHERE name=?", (int(enabled), name))
    db.commit()
    state = "on" if enabled else "off"
    await interaction.response.send_message(
        f"✅ Pokémon-only is now **{state}** for **{name}**." if cur.rowcount
        else f"❌ No store named **{name}**.")


@bot.tree.command(description="Temporarily stop checking a store (use /resume to start again)")
@admin
async def pause(interaction: discord.Interaction, name: str) -> None:
    cur = db.execute("UPDATE stores SET paused=1 WHERE name=?", (name,))
    db.commit()
    await interaction.response.send_message(
        f"⏸️ **{name}** is paused. Use /resume {name} to switch it back on." if cur.rowcount
        else f"❌ No store named **{name}**. Names must match /stores exactly.")


@bot.tree.command(description="Start checking a paused store again")
@admin
async def resume(interaction: discord.Interaction, name: str) -> None:
    cur = db.execute("UPDATE stores SET paused=0 WHERE name=?", (name,))
    db.commit()
    _blocked_until.pop(name, None)  # also clear any rest after a block: try again at once
    _block_count.pop(name, None)
    await interaction.response.send_message(
        f"▶️ **{name}** is back on. It will be checked on the next round." if cur.rowcount
        else f"❌ No store named **{name}**. Names must match /stores exactly.")


@bot.tree.command(description="Remove a store")
@admin
async def removestore(interaction: discord.Interaction, name: str) -> None:
    cur = db.execute("DELETE FROM stores WHERE name=?", (name,))
    db.execute("DELETE FROM products WHERE store=?", (name,))
    db.commit()
    await interaction.response.send_message(
        f"✅ Removed **{name}**." if cur.rowcount else f"❌ No store named **{name}**.")


@bot.tree.command(description="List monitored stores")
async def stores(interaction: discord.Interaction) -> None:
    rows = db.execute("""SELECT s.name, s.url, s.pokemon_only, s.paused, COUNT(p.product_id) AS n FROM stores s
                         LEFT JOIN products p ON p.store = s.name GROUP BY s.name""").fetchall()
    lines = [f"• **{r['name']}**: {r['n']} products ({r['url']})"
             + (" 🔸 Pokémon only" if r["pokemon_only"] else "")
             + (" ⏸️ paused" if r["paused"] else "") for r in rows]
    await interaction.response.send_message("\n".join(lines) or "No stores yet.")


@bot.tree.command(description="Show how the bot is doing: last check and any problems per store")
async def status(interaction: discord.Interaction) -> None:
    lines, all_ok = status_lines()
    e = discord.Embed(title="Bot status", description="\n".join(lines) or "No stores yet.",
                      colour=discord.Colour.green() if all_ok else discord.Colour.orange())
    e.add_field(name="Running since", value=f"<t:{int(STARTED)}:R>")
    e.add_field(name="Alerts sent", value=str(alerts_sent))
    e.add_field(name="Checks every", value=f"{POLL_SECONDS}s shops, {BROWSER_POLL_SECONDS}s browser stores")
    await interaction.response.send_message(embed=e)


# ---------------------------------------------------------------- member sightings
# Online checks can't see stock on physical shelves, so members report it themselves.
SPAM = re.compile(r"https?://|www\.|discord\.gg|@everyone|@here", re.I)


@bot.tree.command(description="Report trading cards you've seen in a physical store")
@app_commands.describe(
    store="Store and branch, e.g. Kmart Sylvia Park",
    product="What you saw, e.g. Pitch Black ETB",
    quantity="Roughly how many (optional)",
    notes="Anything else, e.g. which shelf (optional)")
@app_commands.checks.cooldown(1, 60.0, key=lambda i: i.user.id)
async def sighting(interaction: discord.Interaction,
                   store: app_commands.Range[str, 2, 80],
                   product: app_commands.Range[str, 2, 120],
                   quantity: Optional[app_commands.Range[str, 1, 30]] = None,
                   notes: Optional[app_commands.Range[str, 1, 200]] = None) -> None:
    if any(SPAM.search(f or "") for f in (store, product, quantity, notes)):
        await interaction.response.send_message(
            "❌ Please leave out links and @mentions.", ephemeral=True)
        return
    channel_id = get_setting("sightings_channel_id") or get_setting("channel_id")
    if not channel_id:
        await interaction.response.send_message(
            "❌ No channel set yet. An admin needs to run /setsightings.", ephemeral=True)
        return
    e = discord.Embed(title=f"📍 {product}", colour=discord.Colour.blue(),
                      timestamp=datetime.now(timezone.utc))
    e.add_field(name="Where", value=store, inline=False)
    if quantity:
        e.add_field(name="Quantity", value=quantity)
    if notes:
        e.add_field(name="Notes", value=notes, inline=False)
    e.set_footer(text=f"Reported by {interaction.user.display_name} • unverified, "
                      "check before travelling")
    role_id = get_setting("sightings_role_id")
    ping = f"<@&{role_id}> " if role_id else ""
    channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
    await channel.send(content=f"{ping}**Sighting!**", embed=e,
                       allowed_mentions=discord.AllowedMentions(roles=True))
    now = time.time()
    db.execute("DELETE FROM sightings WHERE ts < ?", (now - 7 * 86400,))
    db.execute("INSERT INTO sightings (ts, user_name, store, product, quantity, notes) "
               "VALUES (?,?,?,?,?,?)",
               (now, interaction.user.display_name, store, product, quantity, notes))
    db.commit()
    log.info("SIGHTING by %s: %s @ %s (qty %s) %s", interaction.user, product, store,
             quantity, notes or "")
    await interaction.response.send_message("✅ Thanks! Your sighting has been posted.",
                                            ephemeral=True)


@bot.tree.command(description="Post member sightings in this channel (optionally pick a role to ping)")
@admin
async def setsightings(interaction: discord.Interaction,
                       role: Optional[discord.Role] = None) -> None:
    set_setting("sightings_channel_id", str(interaction.channel_id))
    set_setting("sightings_role_id", str(role.id) if role else None)
    msg = "✅ Sightings will post in this channel" + (
        f", pinging {role.mention}." if role else " with no ping.")
    await interaction.response.send_message(msg, allowed_mentions=discord.AllowedMentions.none())


KMART_URL = "https://www.kmart.co.nz/category/toys/pokemon-trading-cards/"


def recent_sightings(store_filter: Optional[str], hours: int, limit: int = 10) -> list:
    query, args = "SELECT * FROM sightings WHERE ts >= ?", [time.time() - hours * 3600]
    if store_filter:
        safe = store_filter.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query += " AND LOWER(store) LIKE ? ESCAPE '\\'"
        args.append(f"%{safe}%")
    query += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    return db.execute(query, args).fetchall()


def sighting_text(rows: list) -> str:
    out = []
    for r in rows:
        qty = f" ×{r['quantity']}" if r["quantity"] else ""
        note = f" — {r['notes']}" if r["notes"] else ""
        out.append(f"**{r['product']}**{qty} @ {r['store']}{note}\n"
                   f"<t:{int(r['ts'])}:R> by {r['user_name']} · #{r['id']}")
    return "\n\n".join(out)


@bot.tree.command(description="See recent member sighting reports (optionally for one store)")
@app_commands.describe(store="Only reports whose store contains this, e.g. Kmart",
                       hours="How far back to look (default 24)")
async def sightings(interaction: discord.Interaction,
                    store: Optional[app_commands.Range[str, 1, 40]] = None,
                    hours: app_commands.Range[int, 1, 72] = 24) -> None:
    rows = recent_sightings(store, hours)
    if not rows:
        where = f" for **{store}**" if store else ""
        await interaction.response.send_message(
            f"No sightings reported in the last {hours} hours{where}. "
            "Spotted something? Use /sighting.", ephemeral=True)
        return
    e = discord.Embed(title="Recent sightings" + (f": {store}" if store else ""),
                      description=sighting_text(rows), colour=discord.Colour.blue())
    e.set_footer(text=f"Last {hours} hours • member reports, unverified")
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(description="Kmart's Pokémon page plus the latest Kmart sightings from members")
async def kmart(interaction: discord.Interaction) -> None:
    rows = recent_sightings("kmart", 48)
    desc = f"[Open Kmart's Pokémon trading cards page]({KMART_URL})\n\n"
    if rows:
        desc += "**Member sightings, last 48 hours**\n\n" + sighting_text(rows)
    else:
        desc += "No Kmart sightings reported in the last 48 hours. Seen something? Use /sighting."
    e = discord.Embed(title="Kmart NZ", description=desc, colour=discord.Colour.red())
    e.set_footer(text="Member reports, unverified. Check before travelling")
    await interaction.response.send_message(embed=e)


@bot.tree.command(description="Delete a sighting report by its number (the # shown in /sightings)")
@admin
async def removesighting(interaction: discord.Interaction, number: int) -> None:
    cur = db.execute("DELETE FROM sightings WHERE id=?", (number,))
    db.commit()
    await interaction.response.send_message(
        f"✅ Removed sighting #{number} from the list. Delete its channel post by hand if needed."
        if cur.rowcount else f"❌ No sighting #{number}.", ephemeral=True)


@bot.tree.error
async def on_command_error(interaction: discord.Interaction, error) -> None:
    if isinstance(error, app_commands.CommandOnCooldown):
        msg = f"⏳ You can send another sighting in {int(error.retry_after) + 1}s."
    else:
        log.error("Command error", exc_info=error)
        msg = "❌ Something went wrong. Check the black window for details."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(description="Run a check right now")
@admin
async def checknow(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    n = await run_check("shopify") + await run_check("browser")
    await interaction.followup.send(f"✅ Check complete, {n} alert(s) sent.")


@bot.tree.command(description="Send a fake restock alert to test your setup")
@admin
async def testalert(interaction: discord.Interaction) -> None:
    if not get_setting("channel_id"):
        await interaction.response.send_message("❌ Run /setchannel first.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    store = {"name": "Test Store", "url": "https://example.com"}
    item = {"title": "Test Pokémon Elite Trainer Box", "link": "https://example.com",
            "price": 79.99, "image": None}
    await send_alerts([(store, item)])
    await interaction.followup.send("✅ Test alert sent.", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file")
    init_db()
    bot.run(TOKEN)

    
