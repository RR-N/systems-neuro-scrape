#!/usr/bin/env python3
"""Scrape a public Google Group into monthly JSON files.

Works against the modern Google Groups UI (groups.google.com/g/<name>):
  * The group front page server-renders the first 30 topics plus a
    continuation token inside an AF_initDataCallback JSON blob.
  * Older pages are fetched through the same batchexecute RPC ("Dq0xse")
    the web app uses, which answers anonymous requests.
  * Each topic page server-renders every message body as HTML in its own
    AF_initDataCallback blob.

Output is one JSON per calendar month of topic creation (UTC) —
<out-dir>/messages-YYYY-MM.json — plus <out-dir>/index.json with global
metadata. The shards double as the scraper state: topics whose "messages"
is null are known from the listing but not fetched yet, so a capped run
(--max-fetch) resumes where the previous one stopped. A legacy single-file
messages.json in the output directory is absorbed and removed on the next
run.

Stdlib only — no pip installs needed in CI.
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

BASE = "https://groups.google.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
LIST_RPC_ID = "Dq0xse"  # topic-list pagination RPC of the Groups frontend
PAGE_SIZE = 30

DS_BLOCK_RE = re.compile(r"AF_initDataCallback\(\{key: '(ds:\d+)'.*?data:", re.DOTALL)


# --------------------------------------------------------------------------- http

def http(url, data=None, headers=None, retries=5):
    hdrs = {
        "User-Agent": USER_AGENT,
        # Pre-accepted consent cookie so EU-routed requests don't get the
        # consent interstitial instead of the group page.
        "Cookie": "SOCS=CAI",
    }
    if headers:
        hdrs.update(headers)
    delay = 5
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                print(f"  HTTP {e.code}, retrying in {delay}s ...", flush=True)
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                print(f"  network error ({e.reason}), retrying in {delay}s ...", flush=True)
                time.sleep(delay)
                delay *= 2
                continue
            raise


# ------------------------------------------------------------------- html parsing

def parse_ds_blobs(html):
    """Return {ds_key: parsed_json} for every AF_initDataCallback block."""
    decoder = json.JSONDecoder()
    blobs = {}
    for m in DS_BLOCK_RE.finditer(html):
        try:
            data, _ = decoder.raw_decode(html, m.end())
            blobs[m.group(1)] = data
        except ValueError:
            continue
    return blobs


def find_topic_list(blobs):
    """The listing blob is [group_info, total_count, [topic_entry...], token]."""
    for data in blobs.values():
        if not (
            isinstance(data, list)
            and len(data) >= 3
            and isinstance(data[1], int)
            and isinstance(data[2], list)
        ):
            continue
        if not data[2]:
            if data[1] == 0:
                return data
            continue
        first = data[2][0]
        if (
            isinstance(first, list)
            and first
            and isinstance(first[0], list)
            and len(first[0]) > 6
            and isinstance(first[0][1], str)
            and isinstance(first[0][2], str)
        ):
            return data
    return None


def find_topic_page(blobs, topic_id):
    """The topic blob is [group_info, topic_meta, [message_entry...]]."""
    for data in blobs.values():
        if (
            isinstance(data, list)
            and len(data) == 3
            and isinstance(data[1], list)
            and len(data[1]) > 2
            and data[1][1] == topic_id
            and isinstance(data[2], list)
        ):
            return data
    return None


class _TextExtractor(HTMLParser):
    BLOCK_TAGS = {"p", "div", "br", "li", "tr", "table", "blockquote", "h1", "h2", "h3", "h4"}

    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)

    def text(self):
        raw = "".join(self.parts)
        raw = raw.replace(" ", " ")
        lines = [ln.strip() for ln in raw.splitlines()]
        out, blank = [], 0
        for ln in lines:
            blank = blank + 1 if not ln else 0
            if blank <= 1:
                out.append(ln)
        return "\n".join(out).strip()


def html_to_text(body_html):
    p = _TextExtractor()
    try:
        p.feed(body_html)
    except Exception:
        return ""
    return p.text()


# ------------------------------------------------------------------- redaction

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MAILTO_RE = re.compile(r"""(href=(["']))mailto:[^"']*(\2)""", re.IGNORECASE)
REDACTED = "EMAIL_REDACTED"


def redact_emails(text):
    """Replace email addresses (and whole mailto: hrefs, which may be
    URL-encoded) with EMAIL_REDACTED. Idempotent."""
    if not text:
        return text
    text = MAILTO_RE.sub(lambda m: f"{m.group(1)}mailto:{REDACTED}{m.group(3)}", text)
    return EMAIL_RE.sub(REDACTED, text)


def redact_topic(topic):
    """Scrub every content field of a topic record in place. Safe to apply
    repeatedly, so it runs both at ingestion and on previously stored data."""
    for k in ("title", "snippet"):
        if topic.get(k):
            topic[k] = redact_emails(topic[k])
    if topic.get("authors"):
        topic["authors"] = [redact_emails(a) if a else a for a in topic["authors"]]
    for m in topic.get("messages") or []:
        for k in ("title", "body_html", "body_text", "author"):
            if m.get(k):
                m[k] = redact_emails(m[k])
    return topic


# --------------------------------------------------------------- record building

def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def topic_from_meta(meta, group_name):
    """meta layout: [gid, topic_id, title, snippet, [last_ts, nanos], [created_ts],
    msg_count, ..., [[author...], n], ...]"""
    authors = []
    try:
        for a in meta[9][0]:
            if a and a[0]:
                authors.append(a[0])
    except (IndexError, TypeError):
        pass
    last = meta[4][0] if isinstance(meta[4], list) else None
    created = meta[5][0] if isinstance(meta[5], list) else None
    return redact_topic({
        "id": meta[1],
        "url": f"{BASE}/g/{group_name}/c/{meta[1]}",
        "title": meta[2] or "",
        "snippet": meta[3] or "",
        "created": iso(created) if created else None,
        "created_ts": created,
        "last_activity": iso(last) if last else None,
        "last_activity_ts": last,
        "num_messages": meta[6] if isinstance(meta[6], int) else None,
        "authors": authors,
        "messages": None,  # null = listing known, bodies not fetched yet
    })


def message_from_entry(entry):
    """entry = [msg_meta, body, ...]; msg_meta: [gid, msg_id, [author, [to]],
    0, null, title, snippet, [updated_ts, nanos], [created_ts], ...];
    body: [2, [[1, [null, "<html>"]], ...]]"""
    meta = entry[0]
    author = None
    author_id = None
    try:
        if meta[2] and meta[2][0]:
            author = meta[2][0][0]
            author_id = meta[2][0][3]
    except (IndexError, TypeError):
        pass
    created = None
    updated = None
    try:
        created = meta[8][0]
    except (IndexError, TypeError):
        pass
    try:
        updated = meta[7][0]
    except (IndexError, TypeError):
        pass

    body_html = ""
    try:
        for seg in entry[1][1]:
            if isinstance(seg, list) and seg and seg[0] == 1 and isinstance(seg[1], list):
                body_html += seg[1][1] or ""
    except (IndexError, TypeError):
        pass

    body_html = redact_emails(body_html)
    return {
        "id": meta[1],
        "author": redact_emails(author) if author else author,
        "author_id": author_id,
        "created": iso(created) if created else None,
        "created_ts": created,
        "updated": iso(updated) if updated else None,
        "title": redact_emails(meta[5]) if len(meta) > 5 and meta[5] else None,
        "body_html": body_html,
        # redact again after tag-stripping: an address split across HTML tags
        # reassembles in the text rendering and would slip the HTML-level pass
        "body_text": redact_emails(html_to_text(body_html)),
    }


# ------------------------------------------------------------------ list walking

def rpc_list_page(group_name, group_email, token):
    inner = json.dumps([group_email, PAGE_SIZE, token, [], 2])
    freq = json.dumps([[[LIST_RPC_ID, inner, None, "generic"]]])
    qs = urllib.parse.urlencode(
        {"rpcids": LIST_RPC_ID, "source-path": f"/g/{group_name}", "hl": "en-US"}
    )
    url = f"{BASE}/_/GroupsFrontendUi/data/batchexecute?{qs}"
    body = ("f.req=" + urllib.parse.quote(freq) + "&").encode()
    raw = http(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})

    start = raw.index("[[")
    envelope, _ = json.JSONDecoder().raw_decode(raw, start)
    for item in envelope:
        if isinstance(item, list) and len(item) > 2 and item[0] == "wrb.fr" and item[1] == LIST_RPC_ID:
            return json.loads(item[2])
    raise ValueError("batchexecute response contained no topic-list payload")


def walk_topic_list(group_name, store, quick=False, max_pages=None):
    """Walk the topic listing (newest activity first), upserting topic metadata.

    Returns (group_email, reported_total, changed_ids).
    """
    print(f"Fetching {BASE}/g/{group_name}", flush=True)
    html = http(f"{BASE}/g/{group_name}")
    blobs = parse_ds_blobs(html)
    data = find_topic_list(blobs)
    if data is None:
        raise SystemExit("Could not find the topic-list data blob — page layout may have changed.")

    group_email = data[0][1]
    reported_total = data[1]
    changed = []
    page = 1

    while True:
        page_changed = 0
        for entry in data[2]:
            meta = entry[0]
            rec = topic_from_meta(meta, group_name)
            old = store.get(rec["id"])
            if old is None:
                store[rec["id"]] = rec
                changed.append(rec["id"])
                page_changed += 1
            elif old.get("last_activity_ts") != rec["last_activity_ts"]:
                rec["messages"] = None  # activity changed -> refetch bodies
                store[rec["id"]] = rec
                changed.append(rec["id"])
                page_changed += 1

        token = data[3] if len(data) > 3 else None
        print(f"  list page {page}: {len(data[2])} topics, {page_changed} new/updated", flush=True)

        if not token or not data[2]:
            break
        if quick and page_changed == 0:
            print("  quick mode: page had no changes, stopping list walk", flush=True)
            break
        if max_pages and page >= max_pages:
            print(f"  reached --max-list-pages {max_pages}", flush=True)
            break

        time.sleep(0.5)
        data_next = rpc_list_page(group_name, group_email, token)
        data = [data[0], reported_total, data_next[2], data_next[3] if len(data_next) > 3 else None]
        page += 1

    return group_email, reported_total, changed


# ------------------------------------------------------------------ topic bodies

def fetch_topic_messages(group_name, topic):
    html = http(f"{BASE}/g/{group_name}/c/{urllib.parse.quote(topic['id'])}")
    data = find_topic_page(parse_ds_blobs(html), topic["id"])
    if data is None:
        raise ValueError("no message blob found")
    # data[2] is [[msg_entry, ...]] — the message list sits one level down.
    # Real message entries are [meta_list, body_list, ...]; the container also
    # holds trailer records whose first element is a plain string — skip those.
    entries = data[2][0] if data[2] and isinstance(data[2][0], list) else []
    messages = [
        message_from_entry(e)
        for e in entries
        if isinstance(e, list) and e and isinstance(e[0], list)
    ]
    topic["messages"] = messages
    if topic.get("num_messages") not in (None, len(messages)):
        print(
            f"    note: {topic['id']} listing says {topic['num_messages']} messages, "
            f"page served {len(messages)}",
            flush=True,
        )


# ---------------------------------------------------------------------- storage

def month_key(topic):
    ts = topic.get("created_ts")
    if not ts:
        return "unknown"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")


def load_store(out_dir):
    """Rebuild the topic store from the monthly shards, plus a legacy
    single-file messages.json if one is still around."""
    store = {}

    def rank(t):
        # prefer records with fetched bodies, then the freshest activity
        return (t.get("messages") is not None, t.get("last_activity_ts") or 0)

    files = sorted(out_dir.glob("messages-*.json"))
    legacy = out_dir / "messages.json"
    if legacy.exists():
        files.append(legacy)
    for f in files:
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            print(f"  warning: skipping unparseable {f}", flush=True)
            continue
        for t in doc.get("topics", []):
            old = store.get(t["id"])
            if old is None or rank(t) > rank(old):
                # re-scrub on load so data stored before the redaction pass
                # existed (or before a rule improved) gets cleaned up too
                store[t["id"]] = redact_topic(t)
    return store


# ------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--group", default="systems-neuroscience", help="group name from the groups.google.com/g/<name> URL")
    ap.add_argument("--out-dir", default="data", help="output directory for the monthly JSON shards (also the resume state)")
    ap.add_argument("--max-fetch", type=int, default=500, help="max topic pages to fetch this run (backfill resumes next run)")
    ap.add_argument("--max-list-pages", type=int, default=None, help="cap listing pages walked (debugging)")
    ap.add_argument("--quick", action="store_true", help="stop walking the listing at the first page with no changes")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds to sleep between topic fetches")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    store = load_store(out_dir)
    if store:
        print(f"Loaded {len(store)} topics from {out_dir}", flush=True)

    group_email, reported_total, changed = walk_topic_list(
        args.group, store, quick=args.quick, max_pages=args.max_list_pages
    )

    pending = [t for t in store.values() if t["messages"] is None]
    pending.sort(key=lambda t: t.get("last_activity_ts") or 0, reverse=True)
    print(f"{len(store)} topics known, {len(changed)} new/updated, {len(pending)} pending body fetch", flush=True)

    def save():
        topics = sorted(store.values(), key=lambda t: (t.get("created_ts") or 0, t["id"]))
        remaining = sum(1 for t in topics if t["messages"] is None)

        by_month = {}
        for t in topics:
            by_month.setdefault(month_key(t), []).append(t)

        out_dir.mkdir(parents=True, exist_ok=True)
        months = []
        for month in sorted(by_month):
            shard = by_month[month]
            shard_pending = sum(1 for t in shard if t["messages"] is None)
            fname = f"messages-{month}.json"
            # no scraped_at in shard meta: a month's file only changes when its
            # topics do, so completed months stay byte-identical run to run
            doc = {
                "meta": {
                    "group": args.group,
                    "month": month,
                    "topics": len(shard),
                    "topics_pending_bodies": shard_pending,
                },
                "topics": shard,
            }
            (out_dir / fname).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
            months.append(
                {"month": month, "file": fname, "topics": len(shard), "topics_pending_bodies": shard_pending}
            )

        index = {
            "meta": {
                "group": args.group,
                "group_email": group_email,
                "group_url": f"{BASE}/g/{args.group}",
                "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "topics_reported_by_google": reported_total,
                "topics_in_file": len(topics),
                "topics_pending_bodies": remaining,
                "complete": remaining == 0,
            },
            "months": months,
        }
        (out_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")

        legacy = out_dir / "messages.json"
        if legacy.exists():
            legacy.unlink()
            print(f"  absorbed legacy {legacy} into monthly shards and removed it", flush=True)
        return len(topics), remaining

    fetched = 0
    consecutive_errors = 0
    for topic in pending[: args.max_fetch]:
        try:
            fetch_topic_messages(args.group, topic)
            fetched += 1
            consecutive_errors = 0
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                # topic deleted or restricted since it was listed — record and move on
                topic["messages"] = []
                topic["fetch_error"] = f"HTTP {e.code}"
                print(f"    {topic['id']}: HTTP {e.code}, marking unavailable", flush=True)
            else:
                consecutive_errors += 1
                print(f"    failed {topic['id']}: {e}", flush=True)
        except Exception as e:
            consecutive_errors += 1
            print(f"    failed {topic['id']}: {e}", flush=True)
        if consecutive_errors >= 10:
            print("10 consecutive failures — stopping body fetches for this run.", flush=True)
            break
        if fetched and fetched % 200 == 0:
            save()  # checkpoint so a long backfill can't lose an hour of work
            print(f"  checkpoint: {fetched}/{min(len(pending), args.max_fetch)} fetched", flush=True)
        elif fetched and fetched % 25 == 0:
            print(f"  fetched {fetched}/{min(len(pending), args.max_fetch)} topic pages", flush=True)
        time.sleep(args.delay)

    total, remaining = save()
    print(
        f"Wrote monthly shards to {out_dir} — {total} topics, {fetched} bodies fetched this run, "
        f"{remaining} still pending",
        flush=True,
    )


if __name__ == "__main__":
    main()
