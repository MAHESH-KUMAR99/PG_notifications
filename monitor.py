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
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings()
HERE = os.path.dirname(os.path.abspath(__file__))
SRC_FILE = os.path.join(HERE, "sources.json")
DATA_FILE = os.path.join(HERE, "notices.json")
HTML_FILE = os.path.join(HERE, "pg_notices.html")
CFG_FILE = os.path.join(HERE, "config.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"}

MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
RE_DMY = re.compile(r"(?<!\d)(\d{1,2})\s?[-/.]\s?(\d{1,2})\s?[-/.]\s?(20\d{2}|\d{2})(?!\d)")
RE_DMONY = re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?[\s,-]+" + MON + r"[\s,.-]+(20\d{2})", re.I)
RE_MONDY = re.compile(MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})", re.I)
RE_YMD = re.compile(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)")
RE_URLDATE = re.compile(r"(?<!\d)(20(?:2[4-9]))(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")

GENERIC_LABEL = re.compile(
    r"^(click\s*here|here|view|view\s*document|download|details?|read\s*more|more|link|"
    r"notification|notice|press\s*release|pdf|new|schedule|click|open|view\s*more|"
    r"english|hindi|click here to download|view\s*\(.*\)|download\s*\(.*\)|view\s*more|read\s*more)\W*$", re.I)
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
        r = requests.head(url, headers=HEADERS, timeout=15, verify=False, allow_redirects=True)
        lm = r.headers.get("Last-Modified")
        if lm and r.status_code < 400:
            from email.utils import parsedate_to_datetime
            d = parsedate_to_datetime(lm).date()
            if date(2026, 1, 1) <= d <= date.today() + timedelta(days=1):
                return d
    except Exception:  # noqa: BLE001
        pass
    return None


def fetch(url, pw=False, retries=3):
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
            r = requests.get(url, headers=HEADERS, timeout=30, verify=False)
            if r.status_code == 200:
                return r.content, r.url
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {str(e)[:80]}"
        time.sleep(2 + i * 3)
    raise RuntimeError(last or "fetch failed")


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
        if re.sub(r"^https?://[^/]+/?(index\.(php|html?|aspx))?/?$", "", url, flags=re.I) == "":
            continue
        if JUNK_TEXT.match(label) or (len(label) < 3 and not DOC_HREF.search(href)):
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
        title = clean(re.sub(r"\bNew\b$", "", title))[:300]
        if len(title) < 10:
            continue
        has_doc = any(DOC_HREF.search(l["url"]) for l in links)
        if not (has_doc or NOTICE_WORDS.search(title)):
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
    ("Revised / Extended", r"corrigendum|revised|revision|addendum|amend|extended|extension|postpone|re-?schedul|cancel"),
    ("Result / Allotment", r"result|allot|selected|selection list|admitted"),
    ("Merit / Rank List", r"merit list|rank list|provisional list|final list|list of (eligible|candidates)|eligible candidates"),
    ("Seat Matrix / Vacancy", r"seat matrix|vacan|seats"),
    ("Schedule", r"schedule|time ?table|calendar"),
    ("Registration / Apply", r"registration|apply|application|choice filling|willingness|online|resign|web options?|option entry"),
    ("Fees / Refund", r"\bfees?\b|refund|security (deposit|fee)|tuition"),
    ("Documents / Verification", r"verification|document|certificate|\bnoc\b|pwd|pwbd|medical board|undertaking|proforma|biometric|check slip|ಪರಿಶೀಲನ"),
    ("Merit / Rank List", r"\blist\b|\branks?\b"),
    ("Guidelines / Bulletin", r"guideline|bulletin|prospectus|brochure|manual|instruction|\bfaq\b|information|procedure|\brules?\b"),
    ("Counselling Update", r"counsel+ing|\bround\b|\bphase\b|reporting|dashboard"),
]
TAGS = [(n, re.compile(p, re.I)) for n, p in TAGS]


def tag_of(title):
    for name, rx in TAGS:
        if rx.search(title):
            return name
    return "Notice"


def nid(source, n):
    base = n["links"][0]["url"] + "|" + n["title"]
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
    known = {n["id"]: n for n in data["notices"]}
    now = datetime.now().isoformat(timespec="seconds")
    srcs = [s for s in conf["sources"] if not only or s["id"] in only]
    random.shuffle(srcs)
    order = {x["id"]: i for i, x in enumerate(conf["sources"])}
    deadline = time.time() + 12 * 60  # never let one run hang the whole job
    for s in srcs:
        st = data["sources"].setdefault(s["id"], {})
        if time.time() > deadline:
            print(f"[{s['id']}] skipped (run time budget used up)")
            continue
        try:
            st.update(name=s["name"], url=s.get("view_url") or s["urls"][0], last_checked=now, order=order[s["id"]])
            found, errs = [], []
            for u in s["urls"]:
                try:
                    html, final = fetch(u, pw=s.get("pw", False))
                    found += extract(html, final, s)
                except Exception as e:  # noqa: BLE001
                    errs.append(str(e)[:120])
                time.sleep(random.uniform(1.5, 4))
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
            first_run = not st.get("initialized")
            new = 0
            seen_ids = set()
            head_budget = 20
            for n in found:
                n["source"] = s["id"]
                n["id"] = nid(s["id"], n)
                if n["id"] in seen_ids:
                    continue
                seen_ids.add(n["id"])
                if n["date"] and n["date"] < min_date:
                    continue
                old = known.get(n["id"])
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
            st["count"] = len(seen_ids)
            print(f"[{s['id']}] ok: {len(seen_ids)} notices, {new} new" + (" (baseline)" if first_run else ""))
        except Exception as e:  # noqa: BLE001  one broken source must never stop the others
            st["error"] = f"internal error: {type(e).__name__}: {str(e)[:100]}"
            print(f"[{s['id']}] CRASH {st['error']}")
            note_failure(cfg, s, st)
    for n in data["notices"]:
        n["tag"] = tag_of(n["title"])
    data["last_run"] = now
    data["last_run_utc"] = datetime.utcnow().isoformat(timespec="seconds")
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
        build_html(load())
    else:
        build_html(run([a for a in args if not a.startswith("-")]))
