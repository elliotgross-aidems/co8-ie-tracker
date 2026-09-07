#!/usr/bin/env python3
"""
CO-08 independent expenditure tracker (Rutinel vs. Evans).

Pulls Schedule E independent expenditures from the OpenFEC API for both
candidates, merges the raw e-filed rows (fast, appear within minutes of a
24/48-hour notice) with the processed rows (slower, but exact amounts),
de-duplicates re-reported items, fixes the missing-date problem with a
best-available-date fallback, keeps a CSV as the running spreadsheet, and
emails a digest of anything new.

Usage:
  python tracker.py               normal run (baseline quietly if no state yet)
  python tracker.py --baseline    record everything as seen, send no email
  python tracker.py --dry-run     fetch and report, change nothing, send nothing
  python tracker.py --test-email  send a test digest with totals + recent items

Environment:
  FEC_API_KEY     required (free key from api.data.gov)
  SMTP_USER       Google Workspace address that sends the email
  SMTP_PASSWORD   app password for that address
  RECIPIENTS      comma-separated list of addresses to notify (required)
"""
import csv
import html
import json
import math
import os
import re
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CSV_PATH = DATA / "ie_spending.csv"
STATE_PATH = DATA / "state.json"
TOTALS_PATH = DATA / "totals.json"

API = "https://api.open.fec.gov/v1"
CYCLE = 2026
MIN_DATE = "2025-01-01"  # ignore anything older (Evans has 2024-cycle history)
TOTALS_START = "2026-07-01"  # running totals cover the general election only (primary was June 30)

CANDIDATES = {
    "H6CO08013": {"name": "Manny Rutinel", "short": "Rutinel", "party": "DEM"},
    "H4CO08034": {"name": "Gabe Evans", "short": "Evans", "party": "REP"},
}

CSV_FIELDS = [
    "best_date", "date_source", "candidate", "support_oppose", "side",
    "committee", "amount", "description", "payee", "expenditure_date",
    "dissemination_date", "filed_date", "form", "source", "file_number",
    "pdf_url", "committee_id", "candidate_id", "also_listed_as", "duplicate_of",
    "first_seen", "fingerprint",
]


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- fetching

def api_get(path, params, retries=4):
    key = os.environ.get("FEC_API_KEY")
    if not key:
        sys.exit("FEC_API_KEY is not set")
    params = dict(params, api_key=key)
    url = f"{API}{path}?{urllib.parse.urlencode(params, doseq=True)}"
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:200]
            if e.code == 429 or e.code >= 500:
                wait = 30 * (attempt + 1)
                log(f"HTTP {e.code} on {path}, retrying in {wait}s: {body}")
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {e.code} on {url.replace(key, '***')}: {body}")
        except (urllib.error.URLError, TimeoutError) as e:
            log(f"network error on {path}: {e}; retrying")
            time.sleep(15)
    raise RuntimeError(f"gave up on {path}")


def fetch_processed(candidate_id):
    """Processed Schedule E. Uses keyset pagination (page= is ignored here)."""
    rows, params = [], {
        "candidate_id": candidate_id, "cycle": CYCLE, "per_page": 100,
        "sort": "-expenditure_date", "sort_hide_null": "false",
    }
    while True:
        d = api_get("/schedules/schedule_e/", params)
        rows.extend(d["results"])
        li = (d.get("pagination") or {}).get("last_indexes") or {}
        if not d["results"] or not li.get("last_index"):
            break
        params = dict(params, last_index=li["last_index"])
        if li.get("last_expenditure_date"):
            params["last_expenditure_date"] = li["last_expenditure_date"]
    return rows


def fetch_efile(candidate_id):
    """Raw e-filed Schedule E (24/48-hour notices show up here first)."""
    rows, page = [], 1
    while True:
        d = api_get("/schedules/schedule_e/efile/",
                    {"candidate_id": candidate_id, "per_page": 100, "page": page})
        rows.extend(d["results"])
        if page >= (d.get("pagination") or {}).get("pages", 1) or not d["results"]:
            break
        page += 1
    return rows


# ------------------------------------------------------------- normalizing

def _d(s):
    return s[:10] if s else ""


def norm_desc(s):
    s = (s or "").upper().replace("ESTIMATE", "")
    return re.sub(r"[^A-Z0-9]", "", s)[:24]


def normalize(row, source):
    cid = row.get("candidate_id") or ""
    cand = CANDIDATES.get(cid, {})
    so = (row.get("support_oppose_indicator") or "").upper()
    amount = float(row.get("expenditure_amount") or 0)
    exp = _d(row.get("expenditure_date"))
    diss = _d(row.get("dissemination_date"))
    if source == "processed":
        filed = _d(row.get("filing_date"))
    else:
        filing = row.get("filing") or {}
        filed = _d(filing.get("receipt_date") or row.get("load_timestamp"))
    if diss:
        best, dsrc = diss, "disseminated"
    elif exp:
        best, dsrc = exp, "expenditure"
    else:
        best, dsrc = filed, "filed"
    key_date = diss or exp or filed
    committee = (row.get("committee") or {}).get("name") or row.get("committee_id") or ""
    fingerprint = "|".join([
        row.get("committee_id") or "", cid, so,
        str(int(math.floor(amount + 1e-6))), key_date,
        norm_desc(row.get("expenditure_description")),
    ])
    if so == "S":
        side = f"Pro-{cand.get('short', '?')}"
    elif so == "O":
        other = [c["short"] for k, c in CANDIDATES.items() if k != cid]
        side = f"Pro-{other[0]}" if len(other) == 1 else f"Anti-{cand.get('short', '?')}"
    else:
        side = "?"
    return {
        "best_date": best,
        "date_source": dsrc,
        "candidate": cand.get("name", row.get("candidate_name") or cid),
        "support_oppose": {"S": "Support", "O": "Oppose"}.get(so, so),
        "side": side,
        "committee": committee,
        "amount": f"{amount:.2f}",
        "description": (row.get("expenditure_description") or "").strip(),
        "payee": (row.get("payee_name") or "").strip(),
        "expenditure_date": exp,
        "dissemination_date": diss,
        "filed_date": filed,
        "form": row.get("filing_form") or "",
        "source": source,
        "file_number": str(row.get("file_number") or ""),
        "pdf_url": row.get("pdf_url") or "",
        "committee_id": row.get("committee_id") or "",
        "candidate_id": cid,
        "also_listed_as": "",
        "duplicate_of": "",
        "first_seen": "",
        "fingerprint": fingerprint,
    }


def usable(row):
    if row.get("memo_code") == "X":          # memo / subtotal lines
        return False
    if row.get("most_recent") is False:      # superseded by an amendment
        return False
    if row.get("candidate_id") not in CANDIDATES:
        return False
    best = _d(row.get("dissemination_date")) or _d(row.get("expenditure_date")) or ""
    if best and best < MIN_DATE:
        return False
    return True


def fetch_all():
    records = {}
    for cid in CANDIDATES:
        p = fetch_processed(cid)
        e = fetch_efile(cid)
        log(f"{CANDIDATES[cid]['short']}: {len(p)} processed rows, {len(e)} e-filed rows")
        # e-file first, then processed overwrites (exact amounts, official dates)
        for source, rows in (("efile", e), ("processed", p)):
            for row in rows:
                if usable(row):
                    rec = normalize(row, source)
                    records[rec["fingerprint"]] = rec
    return records


# ------------------------------------------------------------------ state

def load_csv():
    if not CSV_PATH.exists():
        return {}
    with CSV_PATH.open(newline="") as f:
        return {r["fingerprint"]: r for r in csv.DictReader(f)}


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"seen": {}, "last_run": None}


def sort_key(r):
    return (r["best_date"], r["filed_date"], -float(r["amount"]))


def pair_mirrors(rows):
    """
    Many filers report one ad twice: a "Support Evans" line and an identical
    "Oppose Rutinel" line, full amount on each. Pair those up so the second
    copy is flagged as duplicate_of the first and not counted twice in the
    headline totals or the email. Halves that differ by a cent (AFP splits its
    cost 50/50 between the two lines) are different amounts and stay separate.
    """
    for r in rows:
        r["also_listed_as"] = ""
        r["duplicate_of"] = ""
    groups = {}
    for r in rows:
        k = (r["committee_id"], r["best_date"], r["amount"], norm_desc(r["description"]), r["side"])
        groups.setdefault(k, []).append(r)
    for grp in groups.values():
        supports = sorted([r for r in grp if r["support_oppose"] == "Support"], key=lambda r: r["fingerprint"])
        opposes = sorted([r for r in grp if r["support_oppose"] == "Oppose"], key=lambda r: r["fingerprint"])
        for a, b in zip(supports, opposes):
            if a["candidate_id"] != b["candidate_id"]:
                a["also_listed_as"] = f"{b['support_oppose']} {b['candidate']}"
                b["duplicate_of"] = a["fingerprint"]


def primary(rows):
    return [r for r in rows if not r.get("duplicate_of")]


def write_csv(rows):
    DATA.mkdir(exist_ok=True)
    rows = sorted(rows, key=sort_key, reverse=True)
    with CSV_PATH.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})


def totals(rows):
    rows = [r for r in rows if r["best_date"] >= TOTALS_START]
    # per-candidate support/oppose figures count every FEC line, matching fec.gov;
    # the headline pro-X totals and per-spender totals count double-listed items once
    t = {c["short"]: {"support": 0.0, "oppose": 0.0} for c in CANDIDATES.values()}
    by_committee = {}
    pro = {"Pro-Rutinel": 0.0, "Pro-Evans": 0.0}
    for r in rows:
        amt = float(r["amount"])
        short = next((c["short"] for c in CANDIDATES.values() if c["name"] == r["candidate"]), None)
        if short:
            t[short]["support" if r["support_oppose"] == "Support" else "oppose"] += amt
        if r.get("duplicate_of"):
            continue
        bc = by_committee.setdefault(r["committee"], {"Pro-Rutinel": 0.0, "Pro-Evans": 0.0})
        if r["side"] in bc:
            bc[r["side"]] += amt
            pro[r["side"]] += amt
    pro_r, pro_e = pro["Pro-Rutinel"], pro["Pro-Evans"]
    return {
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": TOTALS_START,
        "items": len(rows),
        "items_excluding_double_listed": len(primary(rows)),
        "double_listed_pairs": len(rows) - len(primary(rows)),
        "pro_rutinel_total": round(pro_r, 2),
        "pro_evans_total": round(pro_e, 2),
        "by_candidate": {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in t.items()},
        "by_committee": dict(sorted(by_committee.items(),
                                    key=lambda kv: -(kv[1]["Pro-Rutinel"] + kv[1]["Pro-Evans"]))),
    }


# ------------------------------------------------------------------ email

def money(x):
    return f"${float(x):,.0f}"


def render_email(new_rows, tot, test=False):
    new_rows = sorted(primary(new_rows), key=sort_key, reverse=True)
    new_total = sum(float(r["amount"]) for r in new_rows)
    pro_r_new = sum(float(r["amount"]) for r in new_rows if r["side"] == "Pro-Rutinel")
    pro_e_new = sum(float(r["amount"]) for r in new_rows if r["side"] == "Pro-Evans")

    if test:
        subject = f"[TEST] CO-08 IE tracker is live: {money(tot['pro_rutinel_total'])} pro-Rutinel vs {money(tot['pro_evans_total'])} pro-Evans"
    else:
        subject = f"New IE spending in CO-08: {money(new_total)} ({len(new_rows)} item{'s' if len(new_rows) != 1 else ''})"
        if pro_r_new and pro_e_new:
            subject += f" | {money(pro_r_new)} pro-Rutinel, {money(pro_e_new)} pro-Evans"
        elif pro_r_new:
            subject += " | pro-Rutinel"
        elif pro_e_new:
            subject += " | pro-Evans"

    def td(s, align="left", extra=""):
        return f'<td style="padding:6px 8px;border-bottom:1px solid #e5e5e5;text-align:{align};vertical-align:top;{extra}">{s}</td>'

    def row_html(r):
        color = "#1a56db" if r["side"] == "Pro-Rutinel" else "#c81e1e" if r["side"] == "Pro-Evans" else "#555"
        date_note = "" if r["date_source"] == "disseminated" else f' <span style="color:#888;font-size:11px">({r["date_source"]} date)</span>'
        link = f'<a href="{html.escape(r["pdf_url"])}">{html.escape(r["form"])}</a>' if r["pdf_url"] else html.escape(r["form"])
        return "<tr>" + "".join([
            td(html.escape(r["best_date"]) + date_note, extra="white-space:nowrap"),
            td(f'<b style="color:{color}">{html.escape(r["side"])}</b><br><span style="color:#666;font-size:12px">{html.escape(r["support_oppose"])} {html.escape(r["candidate"])}'
               + (f'<br>also filed as {html.escape(r["also_listed_as"])}' if r["also_listed_as"] else "") + '</span>'),
            td(html.escape(r["committee"])),
            td(money(r["amount"]), "right", "white-space:nowrap;font-weight:600"),
            td(html.escape(r["description"]) + (f'<br><span style="color:#888;font-size:12px">Payee: {html.escape(r["payee"])}</span>' if r["payee"] else "")),
            td(link, extra="white-space:nowrap"),
        ]) + "</tr>"

    header = "".join(f'<th style="text-align:{a};padding:6px 8px;border-bottom:2px solid #333;font-size:12px;color:#333">{h}</th>'
                     for h, a in [("Date", "left"), ("Side", "left"), ("Spender", "left"),
                                  ("Amount", "right"), ("Purpose", "left"), ("Filing", "left")])
    items_table = (f'<table style="border-collapse:collapse;width:100%;font-size:13px">'
                   f"<thead><tr>{header}</tr></thead><tbody>{''.join(row_html(r) for r in new_rows)}</tbody></table>")

    bc = tot["by_candidate"]
    totals_table = f"""
    <table style="border-collapse:collapse;font-size:13px;margin-top:6px">
      <tr><th style="text-align:left;padding:4px 10px"></th><th style="text-align:right;padding:4px 10px">Supporting</th><th style="text-align:right;padding:4px 10px">Opposing</th></tr>
      <tr><td style="padding:4px 10px">Manny Rutinel</td><td style="text-align:right;padding:4px 10px">{money(bc['Rutinel']['support'])}</td><td style="text-align:right;padding:4px 10px">{money(bc['Rutinel']['oppose'])}</td></tr>
      <tr><td style="padding:4px 10px">Gabe Evans</td><td style="text-align:right;padding:4px 10px">{money(bc['Evans']['support'])}</td><td style="text-align:right;padding:4px 10px">{money(bc['Evans']['oppose'])}</td></tr>
    </table>
    <p style="font-size:14px;margin:10px 0 0"><b style="color:#1a56db">Pro-Rutinel total: {money(tot['pro_rutinel_total'])}</b>
       &nbsp;&nbsp;|&nbsp;&nbsp; <b style="color:#c81e1e">Pro-Evans total: {money(tot['pro_evans_total'])}</b></p>"""

    top = list(tot["by_committee"].items())[:8]
    top_rows = "".join(
        f'<tr><td style="padding:3px 10px">{html.escape(k)}</td>'
        f'<td style="text-align:right;padding:3px 10px;color:#1a56db">{money(v["Pro-Rutinel"]) if v["Pro-Rutinel"] else ""}</td>'
        f'<td style="text-align:right;padding:3px 10px;color:#c81e1e">{money(v["Pro-Evans"]) if v["Pro-Evans"] else ""}</td></tr>'
        for k, v in top)
    top_table = f"""<table style="border-collapse:collapse;font-size:12px;margin-top:6px">
      <tr><th style="text-align:left;padding:3px 10px">Spender</th><th style="text-align:right;padding:3px 10px">Pro-Rutinel</th><th style="text-align:right;padding:3px 10px">Pro-Evans</th></tr>{top_rows}</table>"""

    intro = ("This is a test message confirming the tracker is running. Below are the most recent items on file and the running totals."
             if test else
             f"{len(new_rows)} new independent expenditure item{'s' if len(new_rows) != 1 else ''} totaling <b>{money(new_total)}</b> "
             f"appeared on fec.gov since the last check.")
    body = f"""<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#222;max-width:900px">
      <p style="font-size:14px">{intro}</p>
      <h3 style="margin:16px 0 6px;font-size:15px">{'Most recent items' if test else 'New items'}</h3>
      {items_table}
      <h3 style="margin:22px 0 4px;font-size:15px">Running totals, general election (since July 1)</h3>
      {totals_table}
      <h3 style="margin:22px 0 4px;font-size:15px">Top spenders since July 1</h3>
      {top_table}
      <p style="color:#888;font-size:11px;margin-top:22px">
        Source: FEC Schedule E via api.open.fec.gov, raw e-filings plus processed data, de-duplicated so quarterly re-reports of
        24/48-hour notices are not counted twice. Where a filer lists one ad both as supporting one candidate and opposing the
        other, it is shown once and counted once in the pro-Rutinel / pro-Evans totals (the per-candidate supporting/opposing
        figures count every line, matching fec.gov). Date shown is the dissemination date when reported, otherwise the
        expenditure date, otherwise the filing date. Amounts on 24-hour notices are often estimates.
        Full spreadsheet: data/ie_spending.csv in the tracker repo.
      </p></div>"""
    text = "\n".join(f"{r['best_date']}  {r['side']:<12} {money(r['amount']):>12}  {r['committee']}  {r['description']}" for r in new_rows)
    text = (f"{re.sub('<[^>]+>', '', intro)}\n\n{text}\n\nSince July 1: pro-Rutinel {money(tot['pro_rutinel_total'])}, "
            f"pro-Evans {money(tot['pro_evans_total'])}\n")
    return subject, text, body


def send_email(subject, text, html_body):
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASSWORD")
    recipients = [r.strip() for r in (os.environ.get("RECIPIENTS") or "").split(",") if r.strip()]
    if not recipients:
        raise RuntimeError("RECIPIENTS is not set; add it as a repository variable")
    if not user or not pw:
        raise RuntimeError("SMTP_USER / SMTP_PASSWORD not set; cannot send email")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"CO-08 IE Tracker <{user}>"
    msg["To"] = ", ".join(recipients)
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=60) as s:
        s.ehlo()
        s.starttls()
        s.login(user, pw)
        s.send_message(msg)
    log(f"emailed {len(recipients)} recipient(s): {subject}")


# ------------------------------------------------------------------- main

def main(argv):
    baseline = "--baseline" in argv
    dry = "--dry-run" in argv
    test = "--test-email" in argv

    state = load_state()
    existing = load_csv()
    first_run = not state["seen"] and not existing
    if first_run and not dry and not test:
        baseline = True
        log("no prior state found; this run will baseline quietly (no email)")

    fresh = fetch_all()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # merge: fresh data refreshes existing rows, keeps first_seen; unseen rows are new
    merged = dict(existing)
    new_rows = []
    for fp, rec in fresh.items():
        if fp in merged:
            rec["first_seen"] = merged[fp].get("first_seen") or now
        else:
            rec["first_seen"] = now
            if fp not in state["seen"]:
                new_rows.append(rec)
        merged[fp] = rec
    all_rows = list(merged.values())
    pair_mirrors(all_rows)
    tot = totals(all_rows)
    new_primary = primary(new_rows)

    log(f"{len(all_rows)} FEC lines on file ({tot['double_listed_pairs']} double-listed twins), "
        f"{len(new_primary)} new. Pro-Rutinel {money(tot['pro_rutinel_total'])}, pro-Evans {money(tot['pro_evans_total'])}")
    for r in sorted(new_primary, key=sort_key, reverse=True):
        twin = f" (+ {r['also_listed_as']})" if r["also_listed_as"] else ""
        log(f"  NEW {r['best_date']} {r['side']:<12} {money(r['amount']):>12}  {r['committee']}  {r['description']}{twin}  [{r['form']} {r['source']}]")

    if dry:
        return 0

    write_csv(all_rows)
    TOTALS_PATH.write_text(json.dumps(tot, indent=2))

    if test:
        recent = sorted(all_rows, key=sort_key, reverse=True)[:10]
        send_email(*render_email(recent, tot, test=True))
        return 0

    if new_primary and not baseline:
        if not (os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD")):
            # leave state unsaved so these items are emailed once credentials exist
            print("::warning::SMTP_USER / SMTP_PASSWORD not set; "
                  f"{len(new_primary)} new item(s) are waiting to be emailed", flush=True)
            return 0
        send_email(*render_email(new_primary, tot))   # raises on failure -> state not saved -> retried next run
    elif new_rows:
        log(f"baseline: recorded {len(new_rows)} lines without emailing")

    for rec in new_rows:
        state["seen"][rec["fingerprint"]] = now
    state["last_run"] = now
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
