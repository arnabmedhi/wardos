"""Survey: map what this account can open on the portal (DESIGN.md "survey mode")."""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
import re
import time

from .runlog import RunLog

SKIP_TILE = re.compile(
    r"log\s*-?\s*out|sign\s*out|password|reset|profile|my\s*account|help|faq|contact|feedback|language|"
    r"hindi|english|skip\s+to|screen\s*reader|font|^a[+-]?$|notification|delete|remove|download\s+app|"
    r"privacy|terms|sitemap|accessibility|hpr\b|^\s*skip\s*$", re.I)

TILES_JS = r"""
(skip) => {
  const deny = new RegExp(skip, 'i');
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
  const short = s => (s || '').replace(/\s+/g, ' ').trim();
  const zoneOf = e => e.closest('header, .navbar, .header, footer') ? 'header'
                    : (e.closest('nav, aside, .sidebar, [class*=sidebar], [class*=side-nav], [class*=sidenav]') ? 'menu' : 'main');
  document.querySelectorAll('[data-wardos-tile]').forEach(e => e.removeAttribute('data-wardos-tile'));
  const tiles = [], seen = new Set();
  const add = (e) => {
    const text = short(e.innerText || e.getAttribute('aria-label') || e.getAttribute('title') || e.getAttribute('alt'));
    if (!text || text.length < 3 || text.length > 60 || seen.has(text)) return;
    seen.add(text);
    const i = tiles.length;
    e.setAttribute('data-wardos-tile', String(i));
    tiles.push({i, text, zone: zoneOf(e), skip: deny.test(text), href: (e.getAttribute('href') || '').replace(location.origin, '')});
  };
  for (const e of document.querySelectorAll('a, button, [role=button], [role=link]')) if (vis(e)) add(e);
  for (const e of document.querySelectorAll('div, span, li, mat-card, img, h3, h4, h5, p')) {
    if (!vis(e) || getComputedStyle(e).cursor !== 'pointer') continue;
    if (e.closest('a, button, [role=button], [role=link]')) continue;
    add(e);
  }
  return tiles;
}
"""

MODULE_JS = r"""
() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
  const short = s => (s || '').replace(/\s+/g, ' ').trim();
  const mask = s => (s || '').replace(/\d{4,}/g, '{n}');
  const links = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href], [routerlink]')) {
    let href = a.getAttribute('href') || a.getAttribute('routerlink') || '';
    if (href.startsWith(location.origin)) href = href.slice(location.origin.length);
    if (!href || href === '#' || href.startsWith('javascript') || /^https?:/i.test(href) || href.startsWith('mailto')) continue;
    const text = short(a.innerText || a.textContent || a.getAttribute('title'));
    if (!text || text.length > 60) continue;
    const key = text + '|' + href;
    if (seen.has(key)) continue;
    seen.add(key);
    let group = '';
    const sub = a.closest('ul.sub-menu, ul.submenu, .sub-menu, .collapse, .dropdown-menu');
    if (sub && sub.parentElement) {
      const head = sub.parentElement.querySelector('.iocn-link, .link_name, [aria-expanded], .dropdown-toggle');
      group = head ? short(head.innerText).slice(0, 40) : '';
    }
    const zone = a.closest('nav, aside, .sidebar, [class*=sidebar], [class*=side-nav], [class*=sidenav]') ? 'menu'
               : (a.closest('header, .navbar, .header') ? 'header' : 'main');
    links.push({text, href: mask(href), group, zone, visible: vis(a)});
  }
  const headers = Array.from(document.querySelectorAll('.iocn-link, [aria-expanded], .dropdown-toggle'))
    .filter(vis).map(e => short(e.innerText)).filter(t => t && t.length <= 60);
  return {path: mask(location.pathname), title: document.title, links, menu_headers: [...new Set(headers)]};
}
"""

EXPAND_JS = r"""
() => Array.from(document.querySelectorAll(
    'nav .iocn-link, aside .iocn-link, .sidebar .iocn-link, [class*=sidebar] .iocn-link, ' +
    'nav [aria-expanded="false"], aside [aria-expanded="false"], [class*=sidebar] [aria-expanded="false"]'))
  .filter(e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length))
  .map((e, i) => { e.setAttribute('data-wardos-head', String(i)); return i; })
"""

LABEL_KEYS = ("menuName", "menu_name", "menuTitle", "displayName", "display_name", "title", "label", "name", "text")
URL_KEYS = ("url", "menuUrl", "menu_url", "routerLink", "router_link", "route", "path", "link", "href", "pageUrl")
CHILD_KEYS = ("children", "subMenus", "sub_menus", "subMenu", "submenu", "childMenus", "child_menus",
              "child", "items", "menus", "nodes")


def menu_tree(data, depth: int = 0) -> list[dict]:
    if depth > 12:
        return []
    if isinstance(data, list):
        return [node for item in data for node in menu_tree(item, depth + 1)]
    if not isinstance(data, dict):
        return []
    label = next((data[k] for k in LABEL_KEYS if isinstance(data.get(k), str) and data[k].strip()), None)
    url = next((data[k] for k in URL_KEYS if isinstance(data.get(k), str) and data[k].strip()), None)
    child_lists = [data[k] for k in CHILD_KEYS if isinstance(data.get(k), list)]
    if label and (url or child_lists):
        children = [n for lst in child_lists for n in menu_tree(lst, depth + 1)]
        return [{"label": label.strip()[:60], "url": re.sub(r"\d{4,}", "{n}", url or ""), "children": children}]
    return [node for value in data.values() for node in menu_tree(value, depth + 1)]


def tree_lines(nodes: list[dict], indent: int = 0, limit: int = 400) -> list[str]:
    lines: list[str] = []
    for node in nodes:
        if len(lines) >= limit:
            break
        lines.append("  " * indent + f"- {node['label']}" + (f"  ({node['url']})" if node.get("url") else ""))
        lines += tree_lines(node.get("children", []), indent + 1, limit - len(lines))
    return lines


async def _settle(browser, seconds: float = 10) -> None:
    await browser.network_quiet(600, seconds)


async def _visit(keeper, page, tile: dict, log: RunLog, per_tile_s: float) -> dict:
    browser, portal = keeper.browser, keeper.portal
    before_pages = set(browser.context.pages)
    before_url = page.url
    blocked_before = len([e for e in keeper.firewall_log if e.get("decision") == "blocked"])
    entry = {"tile": tile["text"], "zone": tile["zone"]}
    try:
        await page.locator(f'[data-wardos-tile="{tile["i"]}"]').first.click(timeout=8000)
    except Exception as exc:
        entry["error"] = f"click failed: {type(exc).__name__}"
        return entry
    target, end = page, time.monotonic() + min(8.0, per_tile_s)
    while time.monotonic() < end:
        fresh = [p for p in browser.context.pages if p not in before_pages and not p.is_closed()
                 and not p.url.startswith("blob:")]
        if fresh:
            target = fresh[-1]
            break
        if page.url != before_url:
            break
        await asyncio.sleep(0.2)
    entry["opens"] = "new_tab" if target is not page else ("same_tab" if page.url != before_url else "in_place")
    try:
        await target.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    await _settle(browser, min(10.0, per_tile_s))
    try:
        heads = await target.evaluate(EXPAND_JS)
        for index in heads[:25]:
            try:
                await target.locator(f'[data-wardos-head="{index}"]').first.click(timeout=2000, force=True)
                await asyncio.sleep(0.35)
            except Exception:
                continue
        info = await target.evaluate(MODULE_JS)
        entry.update(info)
    except Exception as exc:
        entry["error"] = f"could not read the page: {type(exc).__name__}"
    blocked = len([e for e in keeper.firewall_log if e.get("decision") == "blocked"]) - blocked_before
    if blocked:
        entry["blocked_writes"] = blocked
    if target is not page:
        try:
            await target.close()
        except Exception:
            pass
    log.event("survey_tile", tile=tile["text"], opens=entry.get("opens"), path=entry.get("path"),
              links=len(entry.get("links", [])), blocked=blocked)
    return entry


async def survey(keeper, log: RunLog, *, max_tiles: int = 30, per_tile_s: float = 25) -> dict:
    browser, portal = keeper.browser, keeper.portal
    menu_payloads: list = []

    async def on_response(response):
        try:
            if response.request.resource_type in ("xhr", "fetch") and re.search(r"menu", response.url, re.I):
                if "json" in (response.headers.get("content-type") or ""):
                    menu_payloads.append(await response.json())
        except Exception:
            pass

    browser.context.on("response", on_response)
    page = await portal.go_home(browser, log=log)
    try:
        await page.reload(wait_until="domcontentloaded")
        await _settle(browser, 15)
    except Exception:
        pass
    page = await portal.go_home(browser, log=log)
    tiles = await page.evaluate(TILES_JS, SKIP_TILE.pattern)
    log.say(f"Dashboard: {len(tiles)} clickable item(s); visiting module tiles one by one (read-only)")
    modules, skipped = [], []
    visit = [t for t in tiles if not t["skip"] and t["zone"] != "header"][:max_tiles]
    for tile in tiles:
        if tile not in visit:
            skipped.append({"text": tile["text"], "zone": tile["zone"], "why": "skip-list" if tile["skip"] else
                            ("header item, listed only" if tile["zone"] == "header" else "over the limit")})
    for number, tile in enumerate(visit, 1):
        page = await portal.go_home(browser, log=log)
        current = {t["text"]: t for t in await page.evaluate(TILES_JS, SKIP_TILE.pattern)}
        if tile["text"] not in current:
            modules.append({"tile": tile["text"], "error": "not on the dashboard any more"})
            continue
        log.say(f"  [{number}/{len(visit)}] {tile['text']}")
        modules.append(await _visit(keeper, page, current[tile["text"]], log, per_tile_s))
    browser.context.remove_listener("response", on_response)
    tree = [node for payload in menu_payloads for node in menu_tree(payload)]
    catalog = {"portal": portal.name, "generated": datetime.now().isoformat(timespec="seconds"),
               "dashboard_items": len(tiles), "modules": modules, "not_visited": skipped,
               "menu_api": tree, "menu_api_payloads": len(menu_payloads)}
    for index, payload in enumerate(menu_payloads):
        log.write_json(f"menu-payload-{index}.json", payload)
    return catalog


def catalog_lines(catalog: dict) -> list[str]:
    lines = [f"Portal map ({catalog['portal']}, {catalog['generated']})"]
    quiet = 0
    for module in catalog["modules"]:
        if not any(link["zone"] != "header" for link in module.get("links", [])):
            quiet += 1
            continue
        head = f"* {module['tile']}"
        if module.get("opens"):
            head += f"  [{module['opens']}, {module.get('path', '?')}]"
        if module.get("blocked_writes"):
            head += f"  (tried to send data {module['blocked_writes']}x: blocked)"
        if module.get("error"):
            head += f"  ERROR: {module['error']}"
        lines.append(head)
        groups: dict[str, list[str]] = {}
        for link in module.get("links", []):
            if link["zone"] == "header":
                continue
            groups.setdefault(link["group"] or "(top)", []).append(f"{link['text']} ({link['href']})")
        for group, items in groups.items():
            lines.append(f"    {group}: " + "; ".join(items[:40]))
    if catalog.get("menu_api"):
        lines.append(f"Menu list sent by the portal ({catalog['menu_api_payloads']} response(s)):")
        lines += ["    " + line for line in tree_lines(catalog["menu_api"])]
    if quiet:
        lines.append(f"{quiet} other dashboard item(s) opened no module menus (names kept in the run folder only)")
    if catalog.get("not_visited"):
        reasons: dict[str, int] = {}
        for item in catalog["not_visited"]:
            reasons[item["why"]] = reasons.get(item["why"], 0) + 1
        lines.append("Not visited: " + ", ".join(f"{count} {why}" for why, count in reasons.items()))
    return lines


def save_catalog(settings, catalog: dict) -> str:
    folder = settings.private_dir / "catalog"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{catalog['portal']}.json"
    path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)
