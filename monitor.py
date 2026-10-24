"""PG notices monitor.

Fetches every source in sources.json, pulls out the notice rows (title + links + date),
merges them into notices.json and regenerates pg_notices.html (open it in a browser).

    python monitor.py              # check all sources
    python monitor.py MCC KL       # check only these source ids
    python monitor.py --html-only  # just rebuild the page from notices.json

Optional Telegram alerts: put {"bot_token": "...", "chat_id": "..."} in config.json.
"""
import hashlib
import json
import os
import random
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings()
HERE = os.path.dirname(os.path.abspath(__file__))
SRC_FILE = os.path.join(HERE, "sources.json")
PC_MODE = "--pc" in sys.argv
DATA_FILE = os.path.join(HERE, "notices_pc.json" if PC_MODE else "notices.json")
MERGED_FILE = os.path.join(HERE, "notices_merged.json")
HTML_FILE = os.path.join(HERE, "pg_notices.html")
CFG_FILE = os.path.join(HERE, "config.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"}

MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
RE_DMY = re.compile(r"(?<!\d)(\d{1,2})\s?[-/.]\s?(\d{1,2})\s?[-/.]\s?(20\d{2}|\d{2})(?!\d)")
RE_DMONY = re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?[\s,.-]*" + MON + r"[\s,.-]+(20\d{2})", re.I)
RE_MONDY = re.compile(MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})", re.I)
RE_YMD = re.compile(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)")
RE_URLDATE = re.compile(r"(?<!\d)(20(?:2[4-9]))(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")

GENERIC_LABEL = re.compile(
    r"^(click\s*here|here|view|view\s*document|download|details?|read\s*more|more|link|"
    r"notification|notice|press\s*release|pdf|new|schedule|click|open|view\s*more|"
    r"more|english|hindi|click here to download|view\s*\(.*\)|download\s*\(.*\)|view\s*more|read\s*more)\W*$", re.I)
JUNK_TEXT = re.compile(
    r"^(home|contact( us)?|about( us)?|login|sitemap|site map|terms.*|privacy.*|disclaimer|"
    r"help|faq|skip to.*|screen reader.*|accessibility.*|copyright.*|hyperlink.*|rti|"
    r"website polic.*|tenders?|gallery|careers?|feedback|back|next|previous|top|"
    r"facebook|twitter|youtube|instagram|linkedin|share on .*|print|archive.*|view\s*more|read\s*more|more|all notifications?|view all)$", re.I)
JUNK_HREF = re.compile(
    r"^(javascript:|#|mailto:|tel:)|facebook\.com|twitter\.com|x\.com/|youtube\.com|"
    r"instagram\.com|linkedin\.com|whatsapp", re.I)
DOC_HREF = re.compile(
    r"\.(pdf|docx?|xlsx?|zip|rar)(\?|$)|downloader\.ashx|download|noti_view|"
    r"/uploads/|/pdf|pdf_", re.I)
NOTICE_WORDS = re.compile(
    r"notice|notification|circular|schedule|round|counsell?ing|result|merit|seat|allot|"
    r"registration|corrigendum|revised|extension|extended|list|choice|verification|"
    r"guideline|bulletin|prospectus|vacan|cut[- ]?off|fee|document|upload|last date|"
    r"order|press|advertis|information|reporting|willingness|neet|pg\b|mdms|mds\b",
    re.I)
LEAD_DATE = re.compile(r"^\s*\[?\s*(\d{1,2}[\s.\-/]+[A-Za-z]{3,9}\.?[\s,.\-/]+\d{4}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4})\s*\]?\s*[:\-–]+\s*")
# titles that are menu/button labels rather than notices
NAV_TITLE = re.compile(r"^(verification|notice board|notices?|notifications?|(?:📄\s*)?view\s*pdf|.*\bdashboard)\W*$", re.I)


def tidy_title(t):
    """One normal form for titles: no trailing 'new' badge, no leading date prefix."""
    t = clean(re.sub(r"\bnew\b$", "", t, flags=re.I))
    t = LEAD_DATE.sub("", t) or t
    return clean(t.strip(" -–|:.•"))
OLD_YEAR = re.compile(r"(?<!\d)(20(?:0\d|1\d|2[0-5]))(?!\d)")
NEW_YEAR = re.compile(r"2026|26-27|2026-27")
BAD_CONTAINER = re.compile(r"^(nav|navbar|menu|footer|breadcrumb|breadcrumbs|social|social-links|topbar|accessibility|skip-link)$", re.I)


def clean(t):
    return re.sub(r"\s+", " ", (t or "").replace("\xa0", " ")).strip()


def valid_date(y, m, d):
    try:
        dt = date(y, m, d)
    except ValueError:
        return None
    if date(2025, 1, 1) <= dt <= date.today() + timedelta(days=3):
        return dt
    return None


def find_date(text):
    """First plausible date inside text, or None."""
    best = None
    for m in RE_DMY.finditer(text):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = y + 2000 if y < 100 else y
        dt = valid_date(y, mo, d)
        if dt and (best is None or m.start() < best[0]):
            best = (m.start(), dt)
    for m in RE_DMONY.finditer(text):
        dt = valid_date(int(m.group(3)), MONTHS[m.group(2).lower()[:3]], int(m.group(1)))
        if dt and (best is None or m.start() < best[0]):
            best = (m.start(), dt)
    for m in RE_MONDY.finditer(text):
        dt = valid_date(int(m.group(3)), MONTHS[m.group(1).lower()[:3]], int(m.group(2)))
        if dt and (best is None or m.start() < best[0]):
            best = (m.start(), dt)
    for m in RE_YMD.finditer(text):
        dt = valid_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if dt and (best is None or m.start() < best[0]):
            best = (m.start(), dt)
    return best[1] if best else None


RE_UPLOADS = re.compile(r"/uploads/(20\d{2})/(\d{2})/(20\d{2})(\d{2})(\d{2})")
RE_NAMEDATE = re.compile(r"(?:^|[/_-])(20(?:2[4-9]))(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{0,12}(?:[._-]|$)")


def url_date(url):
    m = RE_UPLOADS.search(url)
    if m:
        return valid_date(int(m.group(3)), int(m.group(4)), int(m.group(5)))
    name = url.split("?")[0].rsplit("/", 1)[-1]
    m = RE_NAMEDATE.search(name) or RE_URLDATE.search(name)
    if m:
        return valid_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def file_date(url):
    """Last-Modified of the linked file (used when neither page nor URL gives a date)."""
    try:
        r = requests.head(url, headers=HEADERS, timeout=8, verify=False, allow_redirects=True)
        lm = r.headers.get("Last-Modified")
        if lm and r.status_code < 400:
            from email.utils import parsedate_to_datetime
            d = parsedate_to_datetime(lm).date()
            if date(2026, 1, 1) <= d <= date.today() + timedelta(days=1):
                return d
    except Exception:  # noqa: BLE001
        pass
    return None


def fetch(url, pw=False, retries=2):
    if pw:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_context(user_agent=UA, ignore_https_errors=True).new_page()
            pg.goto(url, wait_until="networkidle", timeout=60000)
            html = pg.content()
            b.close()
        return html.encode("utf-8"), url
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=(10, 20), verify=False)
            if r.status_code == 200:
                return r.content, r.url
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {str(e)[:80]}"
        time.sleep(2 + i * 3)
    raise RuntimeError(last or "fetch failed")


def api_items(src):
    """Sources whose notice board is filled from a JSON API (Haryana)."""
    r = requests.get(src["api"], headers={**HEADERS, "Accept": "application/json",
                     "Origin": src["site"], "Referer": src["site"] + "/"}, timeout=(10, 20), verify=False)
    r.raise_for_status()
    body = r.json()["body"]
    rows = sorted(body.get("notice", []) + body.get("news", []), key=lambda x: -int(x.get("id", 0)))
    out = []
    for rank, n in enumerate(rows):
        subject, content = clean(n.get("subject")), clean(n.get("content"))
        title = subject if not content or content.lower() == subject.lower() else f"{subject}: {content}"
        d = None
        stamp = n.get("validFrom") or n.get("publishedDate")
        if stamp:
            try:  # stored in UTC, the board shows India dates
                d = (datetime.fromisoformat(stamp.replace("Z", "+00:00")) + timedelta(hours=5, minutes=30)).date()
            except ValueError:
                d = None
        out.append({"title": clean(title)[:300], "date": d.isoformat() if d else None,
                    "date_src": "page" if d else None, "rank": rank,
                    # file links are signed and expire within minutes, so point at the live notice board
                    "links": [{"label": "Open notice board", "url": f"{src['site']}/#{n['id']}"}]})
    return out


def strip_chrome(soup):
    for t in soup(["script", "style", "noscript", "nav", "footer", "select", "option"]):
        t.decompose()
    for t in soup.find_all(["div", "ul", "ol", "section", "aside", "header", "span"]):
        if getattr(t, "decomposed", False):
            continue
        if any(BAD_CONTAINER.match(c) for c in (t.get("class") or [])):
            t.decompose()


def row_of(a):
    """Smallest ancestor that looks like one notice row (has a date, or is tr/li)."""
    node = a
    fallback = None
    for _ in range(6):
        node = node.parent
        if node is None or node.name in ("body", "html", "table", "tbody", "ul", "ol"):
            break
        txt = clean(node.get_text(" "))
        n_links = len(node.find_all("a", href=True))
        if len(txt) > 600 or n_links > 4:
            break
        if find_date(txt):
            return node
        if fallback is None and node.name in ("tr", "li"):
            fallback = node
    return fallback or a.parent


def extract(html, base_url, src):
    soup = BeautifulSoup(html, "lxml")
    strip_chrome(soup)
    keep = re.compile(src["keep"], re.I) if src.get("keep") else None
    drop = re.compile(src["drop"], re.I) if src.get("drop") else None
    rows = {}  # id(row element) -> dict
    order = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        pb = re.match(r"javascript:__doPostBack\('([^']+)'", href)
        if pb and src.get("view_url"):
            # ASP.NET postback link: no direct file URL, so point at the page that lists it
            url = src["view_url"] + "#" + pb.group(1)
        elif not href or JUNK_HREF.search(href):
            continue
        else:
            url = urljoin(base_url, href)
        label = clean(a.get_text(" "))
        label = re.sub(r"^[.\s…]+", "", label) or label
        more_link = bool(src.get("more_is_link")) and re.fullmatch(r"(view |read )?more\W*", label, re.I)
        if re.sub(r"^https?://[^/]+/?(index\.(php|html?|aspx))?/?$", "", url, flags=re.I) == "":
            continue
        if (JUNK_TEXT.match(re.sub(r"[\s.…:>»]+$", "", label)) and not more_link) or (len(label) < 3 and not DOC_HREF.search(href)):
            continue
        row = row_of(a)
        key = id(row)
        if key not in rows:
            rows[key] = {"el": row, "links": []}
            order.append(key)
        if not any(l["url"] == url for l in rows[key]["links"]):
            rows[key]["links"].append({"label": label, "url": url})

    out = []
    for rank, key in enumerate(order):
        r = rows[key]
        links = r["links"]
        rtxt = clean(r["el"].get_text(" "))
        dt = find_date(rtxt)
        dsrc = "page" if dt else None
        if not dt:
            for l in links:
                dt = url_date(l["url"])
                if dt:
                    dsrc = "url"
                    break
        # title
        real = [l for l in links if not GENERIC_LABEL.match(l["label"]) and len(l["label"]) >= 8]
        if len(links) == 1 and real:
            title = real[0]["label"]
        else:
            t = rtxt
            for l in links:
                t = t.replace(l["label"], " ")
            t = clean(RE_DMY.sub(" ", t))
            t = re.sub(r"\b(New|NEW)\b", " ", t)
            t = clean(t.strip(" -|:.•"))
            if len(t) >= 12:
                title = t
            elif real:
                title = real[0]["label"]
            else:
                title = clean(rtxt) or links[0]["label"]
        title = tidy_title(title)[:300]
        if len(title) < 10 or NAV_TITLE.match(title) or GENERIC_LABEL.match(title):
            continue
        has_doc = any(DOC_HREF.search(l["url"]) for l in links)
        if not (has_doc or NOTICE_WORDS.search(title) or src.get("more_is_link")):  # a "More" list holds only notices
            continue
        if keep and not keep.search(title):
            continue
        if drop and drop.search(title):
            continue
        if OLD_YEAR.search(title) and not NEW_YEAR.search(title) and not dt:
            continue
        out.append({"title": title, "links": links[:6], "date": dt.isoformat() if dt else None,
                    "date_src": dsrc, "rank": rank})
    # same file linked twice with different titles -> keep the cleanest (shortest) title
    best = {}
    for n in out:
        k = n["links"][0]["url"]
        if k not in best or len(n["title"]) < len(best[k]["title"]):
            best[k] = n
    return [n for n in out if best[n["links"][0]["url"]] is n]


TAGS = [
    ("Revised / Extended", r"वृद्धि|संशोधित|निरस्त|स्थगित|corrigendum|revised|revision|addendum|amend|extended|extension|postpone|re-?schedul|cancel"),
    ("Result / Allotment", r"आवंटन|परिणाम|चयनित|result|allot|selected|selection list|admitted"),
    ("Merit / Rank List", r"मेरिट|मैरिट|सूची|merit list|rank list|provisional list|final list|list of (eligible|candidates)|eligible candidates"),
    ("Seat Matrix / Vacancy", r"seat matrix|vacan|seats"),
    ("Schedule", r"समय[ -]?सारणी|कार्यक्रम|तिथि|schedule|time ?table|calendar"),
    ("Registration / Apply", r"आवेदन|पंजीयन|पंजीकरण|ऑन-?लाईन|ऑनलाइन|विकल्प|registration|apply|application|choice filling|willingness|online|resign|web options?|option entry"),
    ("Fees / Refund", r"\bfees?\b|refund|security (deposit|fee)|tuition"),
    ("Documents / Verification", r"दस्तावेज|सत्यापन|संवीक्षा|प्रमाण|verification|document|certificate|\bnoc\b|pwd|pwbd|medical board|undertaking|proforma|biometric|check slip|ಪರಿಶೀಲನ"),
    ("Merit / Rank List", r"\blist\b|\branks?\b"),
    ("Guidelines / Bulletin", r"guideline|bulletin|prospectus|brochure|manual|instruction|\bfaq\b|information|procedure|\brules?\b"),
    ("Counselling Update", r"काउंसिलिंग|चरण|राउण्ड|counsel+ing|\bround\b|\bphase\b|reporting|dashboard"),
]
TAGS = [(n, re.compile(p, re.I)) for n, p in TAGS]


def tag_of(title):
    for name, rx in TAGS:
        if rx.search(title):
            return name
    return "Notice"


def nid(source, n):
    base = n["title"] if "#" in n["links"][0]["url"] else n["links"][0]["url"] + "|" + n["title"]
    return source + ":" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


def load():
    for path in (DATA_FILE, DATA_FILE + ".bak"):
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except (ValueError, OSError):
                print("could not read", path, "- trying backup")
    return {"sources": {}, "notices": []}


def load_merged():
    """notices.json (GitHub) + notices_pc.json (this PC, for sites that block GitHub's servers)."""
    def read(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"sources": {}, "notices": []}
    a, b = read(os.path.join(HERE, "notices.json")), read(os.path.join(HERE, "notices_pc.json"))
    pc_ids = set(b["sources"])
    a["notices"] = [n for n in a["notices"] if n["source"] not in pc_ids] + b["notices"]
    for k, v in b["sources"].items():
        a["sources"][k] = v
    a["last_run_pc_utc"] = b.get("last_run_utc")
    try:
        with open(SRC_FILE, encoding="utf-8") as f:
            for s in json.load(f)["sources"]:
                if s["id"] in a["sources"]:
                    a["sources"][s["id"]]["manual"] = bool(s.get("manual"))
                    a["sources"][s["id"]]["url"] = s.get("view_url") or s["urls"][0]
    except (OSError, ValueError, KeyError):
        pass
    return a


def save(data):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    if os.path.exists(DATA_FILE):
        try:
            os.replace(DATA_FILE, DATA_FILE + ".bak")
        except OSError:
            pass
    os.replace(tmp, DATA_FILE)


def note_failure(cfg, s, st):
    st["fails"] = st.get("fails", 0) + 1
    if st["fails"] == 3:  # about 30 min of failures, alert once
        notify(cfg, f"DOWN: {s['name']} failing for 3 checks in a row\n{st.get('error')}\n{st.get('url')}")


def notify(cfg, text):
    if not cfg.get("bot_token") or not cfg.get("chat_id"):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
                      data={"chat_id": cfg["chat_id"], "text": text,
                            "disable_web_page_preview": "true"}, timeout=20)
    except Exception as e:  # noqa: BLE001
        print("telegram failed:", e)


def run(only):
    conf = json.load(open(SRC_FILE, encoding="utf-8"))
    cfg = json.load(open(CFG_FILE)) if os.path.exists(CFG_FILE) else {}
    cfg.setdefault("bot_token", os.environ.get("TG_BOT_TOKEN", ""))
    cfg.setdefault("chat_id", os.environ.get("TG_CHAT_ID", ""))
    min_date = conf.get("min_date", "2026-07-01")
    data = load()
    for n in data["notices"]:  # bring older stored titles to the current normal form (ids follow the title)
        t = tidy_title(n["title"])
        if t != n["title"]:
            n["title"] = t
            n["id"] = nid(n["source"], n)
            data["sources"][n["source"]].setdefault("seen", []).append(n["id"])
    known = {n["id"]: n for n in data["notices"]}
    by_title = {}
    for n in sorted(data["notices"], key=lambda n: n.get("first_seen", ""), reverse=True):
        by_title[(n["source"], n["title"])] = n["id"]  # oldest wins
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")  # always UTC; the page converts to local time
    srcs = [s for s in conf["sources"]
            if (not only or s["id"] in only) and (bool(s.get("via") == "pc") == PC_MODE or only)]
    random.shuffle(srcs)
    order = {x["id"]: i for i, x in enumerate(conf["sources"])}
    deadline = time.time() + 12 * 60  # never let one run hang the whole job

    def grab(s):
        found, errs = [], []
        for u in s["urls"]:
            try:
                if s.get("api"):
                    found += api_items(s)
                    continue
                html, final = fetch(u, pw=s.get("pw", False))
                found += extract(html, final, s)
            except Exception as e:  # noqa: BLE001
                errs.append(str(e)[:120])
            time.sleep(random.uniform(0.5, 1.5))
        return found, errs

    from concurrent.futures import ThreadPoolExecutor, as_completed
    fetched = {}
    ex = ThreadPoolExecutor(max_workers=6)
    futs = {ex.submit(grab, s): s for s in srcs}
    try:
        for f in as_completed(futs, timeout=8 * 60):
            fetched[futs[f]["id"]] = f.result()
            print(f"fetched {futs[f]['id']}", flush=True)
    except Exception as e:  # noqa: BLE001
        print("fetch phase cut short:", type(e).__name__)
    ex.shutdown(wait=False, cancel_futures=True)
    for s in srcs:
        st = data["sources"].setdefault(s["id"], {})
        if time.time() > deadline:
            print(f"[{s['id']}] skipped (run time budget used up)")
            continue
        try:
            st.update(name=s["name"], url=s.get("view_url") or s["urls"][0], last_checked=now, order=order[s["id"]])
            found, errs = fetched.get(s["id"], ([], ["timed out (site too slow or blocking this server)"]))
            if not found and not errs and st.get("count"):
                errs.append(f"found 0 notices (was {st['count']}) - site layout may have changed")
            if errs and not found:
                st["error"] = "; ".join(errs)
                print(f"[{s['id']}] FAILED {st['error']}")
                note_failure(cfg, s, st)
                continue
            st["error"] = "; ".join(errs) if errs else None
            st["last_ok"] = now
            if st.get("fails", 0) >= 3:
                notify(cfg, f"RECOVERED: {s['name']} is reachable again")
            st["fails"] = 0
            first_run = not st.get("initialized") or "seen" not in st  # one-time baseline
            seen = set(st.get("seen", []))
            new = 0
            seen_ids = set()
            head_budget = 12
            for n in found:
                n["source"] = s["id"]
                n["id"] = nid(s["id"], n)
                if n["id"] in seen_ids:
                    continue
                seen_ids.add(n["id"])
                if n["date"] and n["date"] < min_date:
                    continue
                if "#" in n["links"][0]["url"] and n["id"] not in known:
                    prev = by_title.get((s["id"], n["title"]))
                    if prev:
                        n["id"] = prev
                        seen_ids.add(prev)
                old = known.get(n["id"])
                if not old and n["id"] in seen:
                    continue  # already seen on the page before (was filtered as old)
                if not old and not n["date"] and head_budget > 0 and (not first_run or n["rank"] < 25):
                    docs = [l["url"] for l in n["links"] if DOC_HREF.search(l["url"])]
                    if docs:
                        head_budget -= 1
                        fd = file_date(docs[0])
                        if fd:
                            n["date"], n["date_src"] = fd.isoformat(), "file"
                if old:
                    old.update(rank=n["rank"], links=n["links"])
                    if n["date"] and not old.get("date"):
                        old.update(date=n["date"], date_src=n["date_src"])
                    continue
                n["first_seen"] = now
                n["baseline"] = first_run
                known[n["id"]] = n
                data["notices"].append(n)
                if not first_run:
                    new += 1
                    d = n["date"] or "date n/a"
                    notify(cfg, f"NEW | {s['name']} | {tag_of(n['title'])}\n{n['title']}\n{d}\n{n['links'][0]['url']}")
            st["initialized"] = True
            st["seen"] = list(seen | seen_ids)[:5000]
            st["count"] = len(seen_ids)
            print(f"[{s['id']}] ok: {len(seen_ids)} notices, {new} new" + (" (baseline)" if first_run else ""))
        except Exception as e:  # noqa: BLE001  one broken source must never stop the others
            st["error"] = f"internal error: {type(e).__name__}: {str(e)[:100]}"
            print(f"[{s['id']}] CRASH {st['error']}")
            note_failure(cfg, s, st)
    postback = {s["id"] for s in conf["sources"] if s.get("view_url")}
    more_src = {s["id"] for s in conf["sources"] if s.get("more_is_link")}  # their "More" link IS the notice
    kept, seen_titles, seen_links = [], set(), set()
    for n in sorted(data["notices"], key=lambda n: n.get("first_seen", "")):
        k = (n["source"], n["title"])
        lk = (n["source"], n["title"], n["links"][0]["url"])
        junk = n["source"] not in more_src and all(
            JUNK_TEXT.match(re.sub(r"[\s.…:>»]+$", "", l["label"])) for l in n["links"])
        if (junk or NAV_TITLE.match(n["title"]) or GENERIC_LABEL.match(n["title"])
                or (n["source"] in postback and k in seen_titles) or lk in seen_links):
            continue
        seen_titles.add(k)
        seen_links.add(lk)
        kept.append(n)
    data["notices"] = kept
    for n in data["notices"]:
        n["tag"] = tag_of(n["title"])
    data["last_run"] = now
    data["last_run_utc"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    save(data)
    return data


def build_feed(data):
    from email.utils import format_datetime
    from xml.sax.saxutils import escape
    names = {k: v.get("name", k) for k, v in data["sources"].items()}
    def stamp(n):
        d = n.get("date") or n["first_seen"][:10]
        return datetime.fromisoformat(d) if len(d) > 10 else datetime.fromisoformat(d + "T00:00:00")
    items = sorted([n for n in data["notices"] if n.get("date") or not n.get("baseline")],
                   key=lambda n: (n.get("date") or n["first_seen"][:10], n["first_seen"]), reverse=True)[:100]
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0"><channel>',
           "<title>PG Counselling Notices</title><link>.</link>",
           "<description>Latest NEET PG counselling notices from MCC and state authorities</description>"]
    for n in items:
        t = f"[{names.get(n['source'], n['source'])}] {n['title']}"
        out.append("<item><title>%s</title><link>%s</link><guid isPermaLink=\"false\">%s</guid>"
                   "<category>%s</category><pubDate>%s</pubDate></item>" % (
                       escape(t), escape(n["links"][0]["url"]), escape(n["id"]),
                       escape(n.get("tag", "Notice")), format_datetime(stamp(n))))
    out.append("</channel></rss>")
    with open(os.path.join(HERE, "feed.xml"), "w", encoding="utf-8") as f:
        f.write("\n".join(out))


def build_html(data):
    for n in data["notices"]:
        n["tag"] = tag_of(n["title"])
    build_feed(data)
    tpl = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(tpl.replace("/*__DATA__*/null", blob))
    print("page:", HTML_FILE)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--html-only" in args:
        merged = load_merged()
        build_html(merged)
        with open(MERGED_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False)
    elif PC_MODE:
        run([a for a in args if not a.startswith("-")])
    else:
        build_html(run([a for a in args if not a.startswith("-")]))
