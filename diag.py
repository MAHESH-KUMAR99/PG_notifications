"""One-off diagnostic: which routes reach KEA, J&K and Arunachal from GitHub's servers?"""
import json, re, time, urllib.parse
import requests, urllib3
urllib3.disable_warnings()

SITES = {
    "KEA": ("https://cetonline.karnataka.gov.in/kea/pgetmed2026", r"KEA|Karnataka Examinations"),
    "J&K": ("https://www.jkbopee.gov.in/Notifications.aspx", r"JKBOPEE|BOPEE"),
    "AR": ("https://dmetrap.in/", r"Directorate of Medical Education|dmetrap"),
}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")
BROWSER = {
    "User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9", "Accept-Encoding": "gzip, deflate", "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1", "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1", "Cache-Control": "max-age=0",
}


def goog(url):
    u = urllib.parse.urlsplit(url)
    host = u.netloc.replace("-", "--").replace(".", "-")
    return f"https://{host}.translate.goog{u.path or '/'}?_x_tr_sl=auto&_x_tr_tl=en&_x_tr_hl=en&_x_tr_pto=wapp"


ROUTES = {
    "direct": lambda u: (u, {"User-Agent": UA}),
    "direct_full_browser_headers": lambda u: (u, BROWSER),
    "google_translate_proxy": lambda u: (goog(u), {"User-Agent": UA}),
    "jina_reader": lambda u: ("https://r.jina.ai/" + u, {"User-Agent": UA, "X-Return-Format": "html"}),
    "allorigins": lambda u: ("https://api.allorigins.win/raw?url=" + urllib.parse.quote(u, safe=""), {"User-Agent": UA}),
    "corsproxy_io": lambda u: ("https://corsproxy.io/?" + urllib.parse.quote(u, safe=""), {"User-Agent": UA}),
    "codetabs": lambda u: ("https://api.codetabs.com/v1/proxy?quest=" + u, {"User-Agent": UA}),
}

out = {"when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "results": []}
try:
    out["runner_ip"] = requests.get("https://api.ipify.org", timeout=10).text
    out["runner_geo"] = requests.get(f"https://ipinfo.io/{out['runner_ip']}/json", timeout=10).json()
except Exception as e:  # noqa: BLE001
    out["runner_ip_error"] = str(e)[:80]

for site, (url, marker) in SITES.items():
    for name, build in ROUTES.items():
        u, h = build(url)
        t0 = time.time()
        row = {"site": site, "route": name}
        try:
            r = requests.get(u, headers=h, timeout=(10, 25), verify=False)
            body = r.text
            row.update(status=r.status_code, bytes=len(r.content), marker=bool(re.search(marker, body, re.I)),
                       links=len(re.findall(r"<a\s", body, re.I)), pdfs=len(re.findall(r"\.pdf", body, re.I)))
        except Exception as e:  # noqa: BLE001
            row.update(error=f"{type(e).__name__}: {str(e)[:70]}")
        row["secs"] = round(time.time() - t0, 1)
        out["results"].append(row)
        print(row, flush=True)

json.dump(out, open("diag_result.json", "w"), indent=1)
