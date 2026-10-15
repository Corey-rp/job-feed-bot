"""
Job Feed Bot - pulls new internship + new grad roles from public GitHub job lists
and posts them to Discord webhooks, routed by category.

Runs on a schedule via GitHub Actions. Tracks what's already been posted in seen.json.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
SEEN_PATH = os.path.join(ROOT, "seen.json")
DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

CATEGORY_LABELS = {
    "swe": "Software Engineering",
    "frontend": "Front End",
    "data": "Data / AI / ML",
    "quant": "Quant",
    "pm": "Product",
    "hardware": "Hardware",
    "other": "Other",
}
CATEGORY_COLORS = {
    "swe": 0x5865F2,
    "frontend": 0x1ABC9C,
    "data": 0x2ECC71,
    "quant": 0xF1C40F,
    "pm": 0xE67E22,
    "hardware": 0xE74C3C,
    "other": 0x95A5A6,
}
TYPE_LABELS = {"intern": "Internship", "newgrad": "New Grad"}

# Simplify-style category names -> our keys
CATEGORY_MAP = {
    "software": "swe",
    "software engineering": "swe",
    "ai/ml/data": "data",
    "data science, ai & machine learning": "data",
    "hardware": "hardware",
    "hardware engineering": "hardware",
    "quant": "quant",
    "quantitative finance": "quant",
    "product": "pm",
    "product management": "pm",
}

# Used when a source has no category field (classified from the job title)
TITLE_RULES = [
    ("quant", r"\bquant|trad(er|ing)\b"),
    ("pm", r"product manag|\bapm\b|associate product|product analyst|program manag"),
    ("hardware", r"hardware|embedded|firmware|electrical|fpga|asic|silicon|robotics|mechanical"),
    ("data", r"\bdata\b|machine learning|\bml\b|\bai\b|analytics|analyst|scientist|research"),
    ("swe", r"software|developer|engineer|\bswe\b|sde|backend|frontend|full.?stack|devops|security|cloud"),
]


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def fetch_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "job-feed-bot"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            log(f"  fetch failed ({attempt + 1}/{retries}): {e}")
            time.sleep(3 * (attempt + 1))
    return None


def normalize_url(url):
    try:
        parts = urlsplit(url.strip())
        query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith(("utm_", "ref", "source", "gh_src"))]
        return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), urlencode(query), ""))
    except Exception:
        return url


def job_keys(job):
    """Two keys so the same role from two repos only posts once."""
    url_key = "u:" + normalize_url(job.get("url", ""))
    loc = (job.get("locations") or [""])[0] if isinstance(job.get("locations"), list) else ""
    name_key = "n:" + "|".join(
        re.sub(r"\s+", " ", str(x)).strip().lower()
        for x in (job.get("company_name", ""), job.get("title", ""), loc)
    )
    return url_key, name_key


FRONTEND_PATTERN = r"front.?end|\bui\b|ui/ux|ux engineer|react|angular|\bvue\b|web develop|web engineer|javascript|typescript|design engineer"


def categorize(job):
    title = str(job.get("title", "")).lower()
    raw = str(job.get("category") or "").strip().lower()
    mapped = CATEGORY_MAP.get(raw)
    # Front-end roles get their own channel, split out of software
    if mapped in (None, "swe") and re.search(FRONTEND_PATTERN, title):
        return "frontend"
    if mapped:
        return mapped
    for key, pattern in TITLE_RULES:
        if re.search(pattern, title):
            return key
    return "other"


def to_epoch(value):
    if isinstance(value, (int, float)):
        return int(value / 1000) if value > 10**11 else int(value)
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def find_webhook(job_type, category):
    """Most specific secret wins: WEBHOOK_INTERN_SWE > WEBHOOK_SWE > WEBHOOK_INTERN > WEBHOOK_DEFAULT."""
    t, c = job_type.upper(), category.upper()
    for name in (f"WEBHOOK_{t}_{c}", f"WEBHOOK_{c}", f"WEBHOOK_{t}", "WEBHOOK_DEFAULT"):
        val = os.getenv(name, "").strip()
        if val:
            return val
    return None


def build_embed(job, job_type, category, source_name):
    locations = job.get("locations") or []
    loc_text = ", ".join(locations[:4]) + (f" +{len(locations) - 4} more" if len(locations) > 4 else "")
    posted = to_epoch(job.get("date_posted"))
    fields = [
        {"name": "Type", "value": TYPE_LABELS.get(job_type, job_type), "inline": True},
        {"name": "Category", "value": CATEGORY_LABELS[category], "inline": True},
        {"name": "Location", "value": loc_text or "Not listed", "inline": False},
    ]
    sponsorship = job.get("sponsorship")
    if sponsorship and sponsorship != "Other":
        fields.append({"name": "Sponsorship", "value": str(sponsorship), "inline": True})
    terms = job.get("terms") or job.get("season")
    if terms:
        fields.append({"name": "Term", "value": ", ".join(terms) if isinstance(terms, list) else str(terms), "inline": True})
    if posted:
        fields.append({"name": "Posted", "value": f"<t:{posted}:R>", "inline": True})
    return {
        "title": f"{job.get('company_name', 'Unknown')} - {job.get('title', 'Role')}"[:256],
        "url": job.get("url"),
        "color": CATEGORY_COLORS[category],
        "fields": fields,
        "footer": {"text": f"via {source_name}"},
    }


def post_to_discord(webhook, embeds):
    """Discord allows 10 embeds per message; respects rate limits."""
    for i in range(0, len(embeds), 10):
        payload = json.dumps({"username": "Job Feed", "embeds": embeds[i:i + 10]}).encode()
        for attempt in range(5):
            req = urllib.request.Request(
                webhook, data=payload, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "job-feed-bot"},
            )
            try:
                with urllib.request.urlopen(req, timeout=30):
                    pass
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = float(json.loads(e.read() or b"{}").get("retry_after", 2))
                    log(f"  rate limited, waiting {wait:.1f}s")
                    time.sleep(wait + 0.5)
                    continue
                log(f"  Discord error {e.code}: {e.read()[:200]!r}")
                return False
        time.sleep(1.2)  # stay well under webhook limits
    return True


README_PATH = os.path.join(ROOT, "README.md")
CATEGORY_EMOJI = {"swe": "💻", "frontend": "🎨", "data": "🤖", "quant": "📈", "pm": "📱", "hardware": "🔧", "other": "🧩"}


def md_cell(text, limit=None):
    text = re.sub(r"\s+", " ", str(text)).strip().replace("|", "\\|")
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def age_label(posted, now):
    if not posted:
        return "-"
    days = max(0, (now - posted) // 86400)
    return f"{days}d" if days < 30 else f"{days // 30}mo"


def badge(label, value, color):
    label_q = label.replace(" ", "%20").replace("-", "--")
    return f"![{label}](https://img.shields.io/badge/{label_q}-{value}-{color})"


def write_readme(rows, config, now):
    """Rebuilds README.md as a live job board."""
    window = config.get("readme_days", 30) * 86400
    per_section = config.get("readme_rows_per_section", 100)
    enabled = config.get("categories", list(CATEGORY_LABELS))
    recent = [r for r in rows if r["category"] in enabled and (not r["posted"] or now - r["posted"] <= window)]
    recent.sort(key=lambda r: (-r["posted"], r["company"].lower()))

    companies = {r["company"].lower() for r in recent}
    today = sum(1 for r in recent if r["posted"] and now - r["posted"] < 86400)
    updated = datetime.fromtimestamp(now, timezone.utc).strftime("%b %d, %Y %H:%M UTC")

    repo = os.getenv("GITHUB_REPOSITORY", "Corey-rp/job-feed-bot")
    out = [
        '<div align="center">',
        "",
        "# 🎯 Internships & New Grad Job Board",
        "",
        "**Every tech internship and new grad role from the top GitHub job lists, in one place.**",
        "",
        " ".join([
            badge("Open Roles", len(recent), "brightgreen"),
            badge("Posted Today", today, "orange"),
            badge("Companies", len(companies), "blue"),
            f"![Stars](https://img.shields.io/github/stars/{repo}?style=flat&color=yellow)",
        ]),
        "",
        f"_Last updated: {updated}_",
        "",
        "</div>",
        "",
        "## About",
        "",
        "> ⭐ Star this repo if it helps you",
        "",
        "I'm a CS senior graduating in December 2026, and I got tired of checking a bunch of different repos every day "
        "just to see what was new. So I built this. It pulls the major internship and new grad lists together, removes "
        "the duplicates, and posts new roles to my Discord as soon as they show up. It isn't only SWE either: front end, "
        "data/AI, quant, product, and hardware all get their own section.",
        "",
        f"The table updates automatically every 15 minutes and shows roles posted in the last {window // 86400} days, newest first.",
        "",
        "**Want alerts in your own Discord server?** Fork this repo and follow [SETUP.md](SETUP.md). It takes about 5 minutes and it's free.",
        "",
        "🛂 = offers visa sponsorship · 🇺🇸 = requires U.S. citizenship",
        "",
        "## Jump to",
        "",
    ]
    sections = []
    for job_type in ("intern", "newgrad"):
        for cat in enabled:
            section_rows = [r for r in recent if r["type"] == job_type and r["category"] == cat]
            if section_rows:
                sections.append((job_type, cat, section_rows))

    for job_type in ("intern", "newgrad"):
        links = []
        for t, cat, section_rows in sections:
            if t == job_type:
                anchor = f"{t}-{cat}"
                links.append(f"[{CATEGORY_EMOJI[cat]} {CATEGORY_LABELS[cat]} ({len(section_rows)})](#{anchor})")
        if links:
            out.append(f"**{TYPE_LABELS[job_type]}s:** " + " · ".join(links))
            out.append("")

    current_type = None
    for t, cat, section_rows in sections:
        if t != current_type:
            current_type = t
            out += ["---", "", f"# {'🎓' if t == 'intern' else '🚀'} {TYPE_LABELS[t]}s", ""]
        out += [
            f'<a id="{t}-{cat}"></a>',
            "",
            f"## {CATEGORY_EMOJI[cat]} {CATEGORY_LABELS[cat]} {TYPE_LABELS[t]}s",
            "",
            "| Company | Role | Location | Age |",
            "|---|---|---|---|",
        ]
        for r in section_rows[:per_section]:
            locs = r["locations"]
            loc = md_cell(", ".join(locs[:2]) + (f" +{len(locs) - 2}" if len(locs) > 2 else ""), 45) or "-"
            flag = " 🛂" if "offers sponsorship" in r["sponsorship"].lower() else (" 🇺🇸" if "citizen" in r["sponsorship"].lower() else "")
            out.append(
                f"| **{md_cell(r['company'], 40)}** | [{md_cell(r['title'], 80)}]({r['url'].replace(')', '%29')}){flag} | {loc} | {age_label(r['posted'], now)} |"
            )
        if len(section_rows) > per_section:
            out.append("")
            out.append(f"_+{len(section_rows) - per_section} more not shown_")
        out.append("")

    out += [
        "---",
        "",
        "## Sources",
        "",
        "All credit goes to the people who maintain these lists. Go star them too:",
        "",
    ] + [f"- [{s['name']}](https://github.com/{s['url'].split('githubusercontent.com/')[1].split('/dev/')[0]})" for s in config["sources"]] + [
        "",
        "Found a broken link or a closed role? Open an issue.",
        "",
    ]
    with open(README_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    log(f"README updated: {len(recent)} roles, {len(companies)} companies")


def load_seen():
    if os.path.exists(SEEN_PATH):
        with open(SEEN_PATH) as f:
            return json.load(f)
    return {"initialized": False, "keys": {}}


def save_seen(seen, prune_days):
    cutoff = time.time() - prune_days * 86400
    seen["keys"] = {k: v for k, v in seen["keys"].items() if v >= cutoff}
    with open(SEEN_PATH, "w") as f:
        json.dump(seen, f, separators=(",", ":"), sort_keys=True)


def main():
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    seen = load_seen()
    first_run = not seen.get("initialized")
    now = int(time.time())
    max_age = config.get("max_age_days", 3) * 86400
    enabled = set(config.get("categories", list(CATEGORY_LABELS)))

    board = {}  # name_key -> row for the README table
    queue = {}  # webhook -> list of embeds
    counts = {}
    skipped_no_webhook = 0

    for source in config["sources"]:
        log(f"Fetching {source['name']}")
        jobs = fetch_json(source["url"])
        if not isinstance(jobs, list):
            log("  skipped (no data)")
            continue
        new_here = 0
        for job in jobs:
            if not job.get("active", True) or not job.get("is_visible", True) or not job.get("url"):
                continue
            keys = job_keys(job)
            if keys[1] not in board:
                board[keys[1]] = {
                    "type": source["type"],
                    "category": categorize(job),
                    "company": job.get("company_name", ""),
                    "title": job.get("title", ""),
                    "url": job.get("url", ""),
                    "locations": job.get("locations") or [],
                    "posted": to_epoch(job.get("date_posted")),
                    "sponsorship": job.get("sponsorship") or "",
                }
            if any(k in seen["keys"] for k in keys):
                continue
            for k in keys:
                seen["keys"][k] = now
            if first_run:
                continue
            # Don't flood old roles (e.g. when a new source is added)
            posted = to_epoch(job.get("date_posted"))
            if posted and now - posted > max_age:
                continue
            category = categorize(job)
            if category not in enabled:
                continue
            webhook = find_webhook(source["type"], category)
            if not webhook and not DRY_RUN:
                skipped_no_webhook += 1
                continue
            embed = build_embed(job, source["type"], category, source["name"])
            queue.setdefault(webhook or "dry-run", []).append(embed)
            label = f"{TYPE_LABELS[source['type']]} / {CATEGORY_LABELS[category]}"
            counts[label] = counts.get(label, 0) + 1
            new_here += 1
        log(f"  {new_here} new")

    if first_run:
        seen["initialized"] = True
        log(f"First run: recorded {len(seen['keys'])} existing roles. Only new ones will post from now on.")
    else:
        for label, n in sorted(counts.items()):
            log(f"  {label}: {n}")
        if skipped_no_webhook:
            log(f"  {skipped_no_webhook} roles had no matching webhook secret")
        for webhook, embeds in queue.items():
            if DRY_RUN:
                for e in embeds:
                    log(f"  [dry run] {e['title']}  ->  {e['url']}")
            else:
                post_to_discord(webhook, embeds)

    save_seen(seen, config.get("forget_after_days", 180))
    if board:
        write_readme(list(board.values()), config, now)
    log("Done")


if __name__ == "__main__":
    sys.exit(main())