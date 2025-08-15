#!/usr/bin/env python3
"""
mail_ai_local.py — Local email analysis toolkit (privacy-first)

Features
- Ingest Gmail Takeout MBOX into SQLite
- Compute quick stats: top senders, counts per month/year, size, attachments
- Optional local LLM (via Ollama) to summarize emails + suggest labels
  * Requires Ollama running locally (http://localhost:11434) and a model like
    mistral, llama3.1, qwen2.5, etc.

Usage
  # 1) Ingest MBOX into SQLite
  python mail_ai_local.py ingest --mbox "/path/to/All mail Including Spam and Trash.mbox" --db ./mail.db

  # 2) Show stats
  python mail_ai_local.py stats --db ./mail.db
  python mail_ai_local.py top-senders --db ./mail.db --limit 30
  python mail_ai_local.py timeline --db ./mail.db --by month

  # 3) Summarize + auto-label (local LLM via Ollama, optional)
  python mail_ai_local.py summarize --db ./mail.db --model mistral --limit 200

  # 4) Export CSV for analysis in Excel
  python mail_ai_local.py export --db ./mail.db --out mails.csv
"""
import argparse
import os
import sqlite3
import mailbox
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
import re
import json
import datetime
import requests
import csv
from pathlib import Path

def safe_decode(value):
    if value is None:
        return ""
    try:
        # decode encoded headers like =?utf-8?Q?...
        if isinstance(value, bytes):
            value = value.decode('utf-8', errors='replace')
        decoded = str(make_header(decode_header(value)))
        return decoded
    except Exception:
        try:
            return value.decode('utf-8', errors='replace')
        except Exception:
            return str(value)

def extract_text_from_message(msg):
    """Return plain text content; prefer text/plain, fallback to text/html stripped."""
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            ctype = part.get_content_type()
            dispo = (part.get_content_disposition() or '').lower()
            if ctype == 'text/plain' and 'attachment' not in dispo:
                # robustly handle cases where get_payload(decode=True) returns None
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                try:
                    text = payload.decode(part.get_content_charset() or 'utf-8', errors='replace')
                except Exception:
                    text = payload.decode('utf-8', errors='replace')
                parts.append(text)
        if parts:
            return "\n".join(parts)
        # fallback to first text/html part
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == 'text/html':
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                try:
                    html = payload.decode(part.get_content_charset() or 'utf-8', errors='replace')
                except Exception:
                    html = payload.decode('utf-8', errors='replace')
                # naive html to text
                return re.sub(r'<[^>]+>', ' ', html)
        return ""
    else:
        ctype = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload is None:
            return ""
        try:
            text = payload.decode(msg.get_content_charset() or 'utf-8', errors='replace')
        except Exception:
            text = payload.decode('utf-8', errors='replace')
        if ctype == 'text/html':
            text = re.sub(r'<[^>]+>', ' ', text)
        return text

def has_attachments(msg):
    if not msg.is_multipart():
        return False
    for part in msg.walk():
        dispo = (part.get_content_disposition() or '').lower()
        if dispo == 'attachment':
            return True
    return False

def init_db(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS emails (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        msg_id TEXT,
        from_addr TEXT,
        to_addr TEXT,
        cc_addr TEXT,
        bcc_addr TEXT,
        subject TEXT,
        date TEXT,
        ts INTEGER,
        size_bytes INTEGER,
        has_attachments INTEGER,
        body TEXT,
        summary TEXT,
        auto_labels TEXT
    )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON emails(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_from ON emails(from_addr)")

def parse_date(dval):
    if not dval:
        return None, None
    try:
        dt = parsedate_to_datetime(dval)
        if dt.tzinfo:
            ts = int(dt.timestamp())
        else:
            ts = int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())
        return dt.isoformat(), ts
    except Exception:
        return None, None

def ingest_mbox(mbox_path, db_path):
    mbox = mailbox.mbox(mbox_path)
    conn = sqlite3.connect(db_path)
    init_db(conn)
    cur = conn.cursor()
    inserted = 0
    for msg in mbox:
        msg_id = safe_decode(msg.get('Message-ID'))
        from_addr = safe_decode(msg.get('From'))
        to_addr = safe_decode(msg.get('To'))
        cc_addr = safe_decode(msg.get('Cc'))
        bcc_addr = safe_decode(msg.get('Bcc'))
        subject = safe_decode(msg.get('Subject'))
        date_str, ts = parse_date(msg.get('Date'))
        body = extract_text_from_message(msg)
        size_bytes = len(body.encode('utf-8', errors='ignore')) if body else 0
        attach = 1 if has_attachments(msg) else 0

        cur.execute("""
            INSERT INTO emails (msg_id, from_addr, to_addr, cc_addr, bcc_addr, subject, date, ts, size_bytes, has_attachments, body, summary, auto_labels)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
        """, (msg_id, from_addr, to_addr, cc_addr, bcc_addr, subject, date_str, ts, size_bytes, attach, body))
        inserted += 1
        if inserted % 500 == 0:
            conn.commit()
            print(f"Ingested {inserted} emails...")
    conn.commit()
    conn.close()
    print(f"Done. Ingested {inserted} emails into {db_path}")

def stats(db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    total = cur.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    size = cur.execute("SELECT SUM(size_bytes) FROM emails").fetchone()[0] or 0
    attach = cur.execute("SELECT SUM(has_attachments) FROM emails").fetchone()[0] or 0
    first_ts = cur.execute("SELECT MIN(ts) FROM emails").fetchone()[0]
    last_ts = cur.execute("SELECT MAX(ts) FROM emails").fetchone()[0]
    def ts_to_date(ts):
        if ts is None: return None
        return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
    print(f"Total emails: {total}")
    print(f"Total text size: {size/1024/1024:.2f} MB")
    print(f"With attachments: {attach}")
    print(f"Date range: {ts_to_date(first_ts)} to {ts_to_date(last_ts)}")
    conn.close()

def top_senders(db_path, limit=20):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT from_addr, COUNT(*) as c, SUM(size_bytes) as sz
        FROM emails
        GROUP BY from_addr
        ORDER BY c DESC
        LIMIT ?
    """, (limit,)).fetchall()
    print("Top senders:")
    for from_addr, c, sz in rows:
        print(f"{from_addr or '(unknown)'}\t{c}\t{(sz or 0)/1024/1024:.2f} MB")
    conn.close()

def timeline(db_path, by="month"):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    if by == "year":
        q = """
        SELECT strftime('%Y', datetime(ts, 'unixepoch')) as y, COUNT(*)
        FROM emails
        GROUP BY y
        ORDER BY y
        """
    else:
        q = """
        SELECT strftime('%Y-%m', datetime(ts, 'unixepoch')) as ym, COUNT(*)
        FROM emails
        GROUP BY ym
        ORDER BY ym
        """
    for k, c in cur.execute(q):
        print(f"{k}\t{c}")
    conn.close()

def export_csv(db_path, out_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT date, from_addr, to_addr, subject, size_bytes, has_attachments, summary, auto_labels
        FROM emails
        ORDER BY ts DESC
    """)
    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(["date","from","to","subject","size_bytes","has_attachments","summary","auto_labels"])
        for row in rows:
            w.writerow(row)
    conn.close()
    print(f"Exported to {out_path}")

OLLAMA_URL = "http://localhost:11434/api/generate"

def run_ollama(model, prompt, options=None):
    payload = {"model": model, "prompt": prompt, "stream": False}
    if options:
        payload["options"] = options
    r = requests.post(OLLAMA_URL, json=payload, timeout=120)
    r.raise_for_status()
    data = r.json()
    return data.get("response","").strip()

SUMMARY_PROMPT = """Tu es une IA qui résume des e-mails en français et propose des étiquettes de tri.
Voici l'e-mail (texte brut):

---
{body}
---

Objectif:
1) Résume en 1-2 phrases.
2) Propose 1 à 3 labels parmi: [Factures, Bancaire, Santé, Travail, École, RH, Rendez-vous, Technique, Newsletter, Promotions, Réseaux sociaux, Voyage, Livraison, Garanties, Notifications, Perso].
3) Indique si action nécessaire (oui/non) et une action courte.

Réponds en JSON avec les clés: summary, labels, action_needed (bool), action (string).
"""

def summarize_batch(db_path, model="mistral", limit=200, min_chars=200, dry=False):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT id, body FROM emails
        WHERE (summary IS NULL OR summary = '')
        AND length(body) >= ?
        ORDER BY ts DESC
        LIMIT ?
    """, (min_chars, limit)).fetchall()
    print(f"To summarize: {len(rows)} emails")
    updated = 0
    for _id, body in rows:
        body_clip = body[:4000]  # keep prompt small
        prompt = SUMMARY_PROMPT.format(body=body_clip)
        try:
            resp = run_ollama(model, prompt, options={"temperature": 0.2})
            # try parse JSON, fallback to raw
            parsed = None
            try:
                parsed = json.loads(resp)
            except Exception:
                # best-effort JSON extraction
                m = re.search(r'\{.*\}', resp, flags=re.S)
                if m:
                    try:
                        parsed = json.loads(m.group(0))
                    except Exception:
                        parsed = {"summary": resp.strip(), "labels": [], "action_needed": False, "action": ""}
                else:
                    parsed = {"summary": resp.strip(), "labels": [], "action_needed": False, "action": ""}
            summary = parsed.get("summary","")[:1000]
            labels = parsed.get("labels",[])
            if isinstance(labels, list):
                labels_str = ", ".join(labels[:5])
            else:
                labels_str = str(labels)[:200]
            if not dry:
                cur.execute("UPDATE emails SET summary=?, auto_labels=? WHERE id=?", (summary, labels_str, _id))
                updated += 1
                if updated % 20 == 0:
                    conn.commit()
                    print(f"Updated {updated} summaries...")
        except Exception as e:
            print(f"[WARN] id={_id}: {e}")
    conn.commit()
    conn.close()
    print(f"Done. Updated {updated} emails.")

def main():
    ap = argparse.ArgumentParser(description="Local Email AI Toolkit")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_ing = sub.add_parser("ingest", help="Ingest MBOX into SQLite")
    p_ing.add_argument("--mbox", required=True)
    p_ing.add_argument("--db", required=True)

    p_stats = sub.add_parser("stats", help="Show global stats")
    p_stats.add_argument("--db", required=True)

    p_top = sub.add_parser("top-senders", help="Top senders")
    p_top.add_argument("--db", required=True)
    p_top.add_argument("--limit", type=int, default=20)

    p_tl = sub.add_parser("timeline", help="Counts per month or year")
    p_tl.add_argument("--db", required=True)
    p_tl.add_argument("--by", choices=["month","year"], default="month")

    p_exp = sub.add_parser("export", help="Export CSV")
    p_exp.add_argument("--db", required=True)
    p_exp.add_argument("--out", required=True)

    p_sum = sub.add_parser("summarize", help="Summarize + auto-label via Ollama")
    p_sum.add_argument("--db", required=True)
    p_sum.add_argument("--model", default="mistral")
    p_sum.add_argument("--limit", type=int, default=200)
    p_sum.add_argument("--min-chars", type=int, default=200)
    p_sum.add_argument("--dry", action="store_true")

    args = ap.parse_args()

    if args.cmd == "ingest":
        ingest_mbox(args.mbox, args.db)
    elif args.cmd == "stats":
        stats(args.db)
    elif args.cmd == "top-senders":
        top_senders(args.db, args.limit)
    elif args.cmd == "timeline":
        timeline(args.db, args.by)
    elif args.cmd == "export":
        export_csv(args.db, args.out)
    elif args.cmd == "summarize":
        summarize_batch(args.db, model=args.model, limit=args.limit, min_chars=args.min_chars, dry=args.dry)

if __name__ == "__main__":
    main()
