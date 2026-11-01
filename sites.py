"""Kmart NZ + The Warehouse support: a real browser fetches the page, parsers read it."""
import asyncio
import json
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup
from playwright_stealth import Stealth

log = logging.getLogger("restock")

KMART_BASE = "https://www.kmart.co.nz"
WAREHOUSE_BASE = "https://www.thewarehouse.co.nz"
WHITCOULLS_BASE = "https://www.whitcoulls.co.nz"


class BlockedError(Exception):
    """The store showed a block page instead of products."""


# ---------------------------------------------------------------- parsers
def parse_hobbylords(html: str) -> list:
    """Parses Hobby Lords product cards."""
    from bs4 import BeautifulSoup
    import re

    soup = BeautifulSoup(html, "html.parser")
    items = []
    
    for card in soup.select('div.product-card-container'):
        # Find the link
        link_el = card.select_one('a[href*="/products/single/"]')
        if not link_el:
            continue
        
        link = link_el['href']
        if link.startswith('/'):
            link = "https://hobbylords.com" + link
            
        # Extract the product ID from the URL (e.g., /products/single/2026-pokemon...)
        # We use the last part of the URL as the ID
        pid = link.rstrip('/').split('/')[-1]
        
        # Find the title. 
        title_el = card.find('p')
        title = title_el.get_text(strip=True) if title_el else "Unknown Hobby Lords Product"
        
        # Check if it's in stock by looking for "Add to Cart"
        # If the button is missing or disabled, it might be out of stock
        in_stock = bool(card.find(string=re.compile("Add to Cart", re.I)))
        
        # Price extraction (look for the $ symbol)
        price_text = card.find(string=re.compile(r"\$\d+\.\d{2}"))
        price = None
        if price_text:
            price = float(re.search(r"\$(\d+\.\d{2})", price_text).group(1))
            
        # Find image
        img = card.find('img')
        img_src = img.get('src') if img else None

        items.append({
            "id": pid,
            "title": title,
            "link": link,
            "in_stock": in_stock,
            "price": price,
            "image": img_src
        })
        
    return items

def _kmart_image(anchor) -> Optional[str]:
    node = anchor
    for _ in range(5):  # the photo sits a few levels above the product link
        node = node.parent
        if node is None:
            return None
        img = node.find("img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            return src if src.startswith("http") else None
    return None
    
def parse_farmers(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    items, seen = [], set()
    
    # Target the exact product tile class
    for tile in soup.select('div.product-tile'):
        # The ID is usually in data-construct-item-id
        pid = tile.get('data-construct-item-id') or tile.get('data-product-id')
        if not pid or pid in seen:
            continue
        
        # Find the title link
        title_el = tile.select_one('a.product-title')
        if not title_el:
            continue
        seen.add(pid)
        
        title = title_el.get_text(strip=True)
        
        # Find the price (usually in a span with product-price or similar)
        price_el = tile.select_one('.product-price, .price, [data-price]')
        price = None
        if price_el:
            match = re.search(r"[\d,]+\.\d{2}", price_el.get_text())
            if match:
                price = float(match.group().replace(",", ""))
                
        # Determine stock status
        tile_text = tile.get_text(" ", strip=True).lower()
        in_stock = "out of stock" not in tile_text and "sold out" not in tile_text
        
        img = tile.find("img")
        src = (img.get("src") or img.get("data-src") or "") if img else ""
        
        link_url = title_el["href"]
        if link_url.startswith("/"):
            link_url = "https://www.farmers.co.nz" + link_url
            
        items.append({
            "id": pid,
            "title": title,
            "link": link_url,
            "in_stock": in_stock,
            "price": price,
            "image": src if src.startswith("http") else None,
        })
    return items
 
 
def parse_kmart(html: str) -> list:
    """Kmart shows 'Out of Stock Online' / 'In Store Only' on a tile when it can't be
    bought online. No label is assumed to mean available online."""
    soup = BeautifulSoup(html, "html.parser")
    items, seen = [], set()
    for a in soup.select('a[href^="/product/"]'):
        href = a["href"]
        name = a.find("p")
        price_el = a.select_one('[data-testid="product-price"]')
        m = re.search(r"(\d+)/?$", href)
        if not (name and price_el and m) or href in seen:
            continue
        seen.add(href)
        callout = a.select_one('[data-testid="message-callout"]')
        label = callout.get_text(" ", strip=True).lower() if callout else ""
        parts = [p for p in price_el.stripped_strings if p != "$" and p.lower() != "ea"]
        price = None
        if parts:
            try:
                price = float(parts[0].replace(",", "") + ("." + parts[1] if len(parts) > 1 else ""))
            except ValueError:
                pass
        items.append({
            "id": int(m.group(1)),
            "title": name.get_text(" ", strip=True),
            "link": KMART_BASE + href,
            "in_stock": "out of stock" not in label and "in store only" not in label,
            "price": price,
            "image": _kmart_image(a),
        })
    return items


def parse_warehouse(html: str) -> list:
    """Each Warehouse tile carries data-stock-status (e.g. IN_STOCK) and data-orderable."""
    soup = BeautifulSoup(html, "html.parser")
    items, seen = [], set()
    for tile in soup.select("div.product-tile[data-pid]"):
        pid = tile["data-pid"]
        link = tile.find("a", href=re.compile(r"^/p/"))
        if pid in seen or not link:
            continue
        seen.add(pid)
        title = None
        try:
            title = json.loads(tile.get("data-ga-product", "")).get("item_name")
        except ValueError:
            pass
        title = title or link.get_text(" ", strip=True)
        if not title:
            continue
        stock = tile.select_one(".availability-stock-status")
        in_stock = bool(stock) and stock.get("data-stock-status") == "IN_STOCK" \
            and stock.get("data-orderable") != "false"
        price = None
        price_el = tile.select_one('[class*="price"]')
        pm = re.search(r"\$\s*(\d[\d,]*(?:\.\d+)?)", price_el.get_text(" ", strip=True)) if price_el else None
        if pm:
            price = float(pm.group(1).replace(",", ""))
        img = tile.find("img")
        src = (img.get("src") or img.get("data-src") or "") if img else ""
        items.append({
            "id": pid,
            "title": title,
            "link": WAREHOUSE_BASE + link["href"],
            "in_stock": in_stock,
            "price": price,
            "image": src if src.startswith("http") else None,
        })
    return items


def parse_whitcoulls(html: str) -> list:
    """Whitcoulls tiles show an 'Add To Cart' button when a product can be bought online,
    and 'In Store Only / View Details' when it can't."""
    soup = BeautifulSoup(html, "html.parser")
    items, seen = [], set()
    for tile in soup.select("div.s-product"):
        name = tile.select_one("a.s-product__name")
        href = name.get("href", "") if name else ""
        m = re.search(r"-(\d{6,})$", href)
        if not m or href in seen:
            continue
        seen.add(href)
        price = None
        price_el = tile.select_one(".s-product__price")
        pm = re.search(r"\$\s*(\d[\d,]*(?:\.\d+)?)", price_el.get_text(" ", strip=True)) if price_el else None
        if pm:
            price = float(pm.group(1).replace(",", ""))
        btn = tile.select_one("button.s-product__add-to-cart")
        img = tile.find("img")
        src = (img.get("data-src") or img.get("src") or "") if img else ""
        items.append({
            "id": int(m.group(1)),
            "title": name.get_text(" ", strip=True),
            "link": WHITCOULLS_BASE + href,
            "in_stock": bool(btn) and btn.get("disabled") is None,
            "price": price,
            "image": src if src.startswith("http") else None,
        })
    return items


# ---------------------------------------------------------------- browser
class BrowserPool:
    """One real, visible Chromium window reused for every check (hidden browsers get
    blocked by both stores). Leave the window open, and don't minimise it."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._context = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> None:
        if self._browser and self._browser.is_connected():
            return
        from playwright.async_api import async_playwright  # imported late on purpose
        if self._pw is None:
            self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
                headless=False,
                channel="chrome"
        )
        self._context = await self._browser.new_context(
            locale="en-NZ", timezone_id="Pacific/Auckland",
            viewport={"width": 1280, "height": 900})
        self._stealth = Stealth()
        log.info("Browser window opened (leave it open)")

    async def _load_everything(self, page) -> None:
        """Scroll down in small steps (that's what triggers lazy loading) and click
        'Show more' buttons until the product count stops growing."""
        more = page.locator("button, a").filter(
            has_text=re.compile(r"(show|load|view) more", re.I))
        last, still = -1, 0
        for _ in range(80):
            try:
                if await more.count() and await more.first.is_visible():
                    await more.first.click(timeout=3000)
                    await page.wait_for_timeout(1500)
            except Exception:
                pass
            await page.evaluate("window.scrollBy(0, 900)")
            await page.wait_for_timeout(800)
            n = await page.locator('a[href^="/product/"], div.product-tile').count()
            still = still + 1 if n == last else 0
            if still >= 5:  # nothing new after five more scrolls: we're at the end
                break
            last = n

    async def get_html(self, url: str, scroll: bool = True) -> str:
        async with self._lock:  # one page at a time
            await self._ensure()
            page = await self._context.new_page()
            await self._stealth.apply_stealth_async(page)
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                if resp and resp.status >= 400:
                    raise BlockedError(f"HTTP {resp.status}")
                await page.wait_for_timeout(5000)
                if scroll:
                    await self._load_everything(page)
                title = await page.title()
                if re.search(r"access denied|attention required|just a moment", title, re.I):
                    raise BlockedError(title)
                return await page.content()
            finally:
                await page.close()

    async def get_items_scrolling(self, url: str, parser) -> list:
        """Scroll a long page and collect products as they show up. Some pages only keep
        the products currently on screen (older ones get dropped again), so looking at
        the page once at the end would miss most of them."""
        async with self._lock:
            await self._ensure()
            page = await self._context.new_page()
            await self._stealth.apply_stealth_async(page)
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                if resp and resp.status >= 400:
                    raise BlockedError(f"HTTP {resp.status}")
                await page.wait_for_timeout(5000)
                title = await page.title()
                if re.search(r"access denied|attention required|just a moment", title, re.I):
                    raise BlockedError(title)
                text = await page.evaluate("document.body.innerText")
                m = re.search(r"\d+\s*-\s*\d+\s+of\s+(\d+)", text or "") \
                    or re.search(r"(\d[\d,]*)\s+items", text or "")
                total = int(m.group(1).replace(",", "")) if m else None  # "1-45 of 45" / "19 items"
                more = page.locator("button, a").filter(
                    has_text=re.compile(r"(show|load|view) more", re.I))
                found, last, still = {}, -1, 0
                for _ in range(80):
                    for it in parser(await page.content()):
                        found[it["id"]] = it
                    if total and len(found) >= total:
                        break
                    still = still + 1 if len(found) == last else 0
                    if still >= 5:  # five scrolls with nothing new: that's the end
                        break
                    last = len(found)
                    try:
                        if await more.count() and await more.first.is_visible():
                            await more.first.click(timeout=3000)
                            await page.wait_for_timeout(1500)
                    except Exception:
                        pass
                    await page.evaluate("window.scrollBy(0, 700)")
                    await page.wait_for_timeout(1300)  # unhurried, like a person reading
                return list(found.values())
            finally:
                await page.close()

    async def warehouse_items(self, url: str) -> list:
        """The Warehouse shows 32 products per page and pages with &start=32, 64...
        Read the first page, then keep going until we have the stated total."""
        base = re.sub(r"[?&]start=\d+", "", url)
        sep = "&" if "?" in base else "?"
        first = await self.get_html(base, scroll=False)
        items, seen = [], set()
        for it in parse_warehouse(first):
            seen.add(it["id"])
            items.append(it)
        text = BeautifulSoup(first, "html.parser").get_text(" ", strip=True)
        m = re.search(r"(\d[\d,]*)\s+products", text)
        total = int(m.group(1).replace(",", "")) if m else len(items)
        start = 32
        while start < total and start <= 320:
            html = await self.get_html(f"{base}{sep}start={start}", scroll=False)
            new = [it for it in parse_warehouse(html) if it["id"] not in seen]
            if not new:
                break
            for it in new:
                seen.add(it["id"])
                items.append(it)
            start += 32
        return items

    async def close(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        
async def get_farmers_items_browser(url: str) -> list:
    """Fetches Farmers using the ScrapingAnt API."""
    import requests
    import asyncio

    # --- CONFIGURATION ---
    API_KEY = "YOUR_API_KEY_HERE"
    # ---------------------

    loop = asyncio.get_running_loop()

    def _fetch():
        params = {
            "x-api-key": API_KEY,
            "url": url,
            "browser": "true",       # Required for Farmers
            "proxy_type": "datacenter",
            "wait_for_selector": "div.product-tile" # Waits for product grid to load
        }
        return requests.get("https://api.scrapingant.com/v2/general", params=params, timeout=90)

    try:
        response = await loop.run_in_executor(None, _fetch)
        
        if response.status_code != 200:
            raise BlockedError(f"ScrapingAnt API error: {response.status_code}")
            
        html = response.text
        
        if "Access Denied" in html or "temporarily down" in html:
            raise BlockedError("Akamai block page detected via ScrapingAnt")

        return parse_farmers(html)

    except Exception as e:
        if isinstance(e, BlockedError):
            raise
        raise BlockedError(f"ScrapingAnt fetch failed: {str(e)}")
        
async def get_kmart_items_constructor(url: str) -> list:
    """Fetches Kmart using the free Constructor.io API."""
    import requests
    import asyncio

    # --- CONFIGURATION ---
    CONSTRUCTOR_KEY = "find_the_key"
    # ---------------------

    loop = asyncio.get_running_loop()

    def _fetch():
        # Use the exact Browse endpoint the website uses for the category
        api_url = "https://ac.cnstrc.com/browse/group_id/2536f6e6e4647ac7e87ea808ec291f1c"
        params = {
            "key": CONSTRUCTOR_KEY,
            "num_results_per_page": 100,
            "page": 1,
            "sort_by": "relevance",
            "sort_order": "descending"
        }
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        return requests.get(api_url, params=params, headers=headers, timeout=30)

    try:
        response = await loop.run_in_executor(None, _fetch)
        
        if response.status_code != 200:
            raise BlockedError(f"Constructor API error: {response.status_code}")
            
        data = response.json()
        
        results = data.get("results", [])
        print("=== KMART DEBUG: API returned", len(results), "results ===")
        
        items = []
        for product in results:
            data_dict = product.get("data", {})
            title = product.get("value", "Unknown Kmart Product")
            pid = data_dict.get("id")
            link_url = data_dict.get("url", "")
            
            if not pid or not link_url:
                continue
                
            link = f"https://www.kmart.co.nz{link_url}"
            
            items.append({
                "id": pid,
                "title": title,
                "link": link,
                "in_stock": True,
                "price": None,
                "image": None,
            })
            
        return items

    except Exception as e:
        if isinstance(e, BlockedError):
            raise
        raise BlockedError(f"Constructor fetch failed: {str(e)}")
        
async def get_hobbylords_items_browser(url: str) -> list:
    """Fetches Hobby Lords using the real browser."""
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth
    
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=False, 
                channel="chrome",
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = await browser.new_context(locale="en-NZ", timezone_id="Pacific/Auckland")
            page = await context.new_page()
            
            stealth = Stealth()
            await stealth.apply_stealth_async(page)
            
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(5000) # Let products load
            
            html = await page.content()
            await browser.close()
            
            if "Access Denied" in html or "Cloudflare" in html:
                raise BlockedError("Hobby Lords blocked the browser")
                
            return parse_hobbylords(html)
    except Exception as e:
        raise BlockedError(f"Fetch failed: {str(e)}")
        
async def get_kmart_items_scrapingant(url: str) -> list:
    """Fetches Kmart using ScrapingAnt (Cheap Mode)."""
    import requests
    import asyncio

    # --- PASTE YOUR SCRAPINGANT KEY HERE ---
    API_KEY = "insert_your_API_KEY"
    # --------------------------------------

    loop = asyncio.get_running_loop()

    def _fetch():
        params = {
            "x-api-key": API_KEY,
            "url": "https://www.kmart.co.nz/category/toys/pokemon-trading-cards/",
            "browser": "false",  # 1-credit mode
            "proxy_type": "datacenter"
        }
        return requests.get("https://api.scrapingant.com/v2/general", params=params, timeout=60)

    try:
        response = await loop.run_in_executor(None, _fetch)
        
        if response.status_code != 200:
            raise BlockedError(f"API error: {response.status_code}")
            
        html = response.text
        
        if "Access Denied" in html or "HTTP 403" in html:
            raise BlockedError("Akamai block page detected via API")

        return parse_kmart(html)

    except Exception as e:
        if isinstance(e, BlockedError):
            raise
        raise BlockedError(f"API fetch failed: {str(e)}")
