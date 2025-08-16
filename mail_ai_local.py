#!/usr/bin/env python3
"""
mail_ai_local_full.py — Local email analysis toolkit (privacy-first)

Fonctions clés :
- Ingestion MBOX -> SQLite (idempotent, UNIQUE msg_id)
- Stats, top-senders, timeline
- Export CSV (UTF-8) et export-excel (UTF-8 BOM pour Excel)
- Résumé + auto-label via Ollama (local) avec réglages de perf
- Logs détaillés + JSON strict côté Ollama, fallback heuristique + timeout
- Déduplication (From+Sujet) : ne traite qu’un exemplaire et propage aux doublons
- Heuristiques rapides par domaine/sujet (auto-label sans IA)
- Compteurs intégrés (count)
- Auto-label de masse sans IA (auto-label)

Exemples :
  python mail_ai_local_full.py ingest --mbox "All-mail.mbox" --db mail.db
  python mail_ai_local_full.py stats --db mail.db
  python mail_ai_local_full.py export-excel --db mail.db --out mails_utf8.csv
  python mail_ai_local_full.py count --db mail.db --what summarized
  python mail_ai_local_full.py auto-label --db mail.db --limit 5000
  python mail_ai_local_full.py summarize --db mail.db --model mistral --limit 100 --clip 800 --num-predict 64 --num-ctx 1024 --temp 0.1 --skip-duplicates --out-jsonl summaries.jsonl
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
import time
import unicodedata
import hashlib
from pathlib import Path

# ----------------- Règles heuristiques -----------------

EMAIL_RE = re.compile(r'[\w\.-]+@[\w\.-]+\.\w+')

DOMAIN_RULES = [
    (r'paypal\.(fr|com)',                ['Bancaire','Factures']),
    (r'linkedin\.com',                   ['Réseaux sociaux']),
    (r'news\.leboncoin\.fr|leboncoin',   ['Promotions','Newsletter']),
    (r'notice\.aliexpress\.com|aliexpress', ['Promotions']),
    (r'(binance|uphold)\.',              ['Finance']),
    (r'(google|accounts\.google)\.',     ['Notifications','Sécurité']),
    (r'news\.veepee\.fr|veepee',         ['Promotions']),
    (r'(twitch\.tv|steampowered\.com)',  ['Technique','Notifications']),
    (r'shopmail\.samsung\.com|samsung',  ['Commandes','Livraison']),
    (r'cds\.newsletter-cdiscount\.com|cdiscount', ['Newsletter','Promotions']),
    (r'microsoftstore\.microsoft\.com|microsoft-noreply@|billing\.microsoft\.com', ['Factures','Bancaire']),
    (r'mail\.toogoodtogo\.fr',          ['Promotions']),
    (r'email\.carrefour\.fr',           ['Promotions']),
    (r'newsletter\.volotea\.com|volotea', ['Voyage','Promotions']),
    (r'quora\.com',                     ['Newsletter']),
    (r'google-gemini-noreply@|gemini',  ['Technique','Notifications']),

]

SUBJECT_RULES = [
    # facturation/paiements
    (r'\b(remboursement|remboursé|refund)\b',            ['Bancaire']),
    (r'\b(paiement|facture|re[çc]u|[ée]ch[ée]ance|invoice|receipt|payment)\b', ['Factures','Bancaire']),
    (r'\b(achat|abonnement|subscription)\b',             ['Factures']),
    # commandes/livraisons
    (r'\b(commande|order|merci pour votre commande)\b',  ['Commandes']),
    (r'\b(livraison|exp[ée]dition|colis|tracking)\b',    ['Livraison']),
    # sécurité / codes
    (r'\b(code|v[ée]rification|2fa|security code)\b',    ['Technique','Notifications']),
    # promos/newsletters
    (r'\b(newsletter|s[ée]lection|promo|promotion|ventes? priv[ée]es?|remise|r[ée]duction|bon(s?) plan(s?))\b',
     ['Newsletter','Promotions']),
]

# ----------------- Helpers généraux -----------------

def safe_decode(value):
    if value is None:
        return ""
    try:
        if isinstance(value, bytes):
            value = value.decode('utf-8', errors='replace')
        decoded = str(make_header(decode_header(value)))
        return decoded
    except Exception:
        try:
            return value.decode('utf-8', errors='replace')
        except Exception:
            return str(value)

def extract_email(address: str):
    if not address:
        return None
    m = EMAIL_RE.findall(address)
    return m[-1].lower() if m else None

def domain_of(addr: str):
    return addr.split('@',1)[1].lower() if addr and '@' in addr else None

def _normalize_text(s: str) -> str:
    s = (s or '').lower()
    s = ''.join(c for c in unicodedata.normalize('NFKD', s) if not unicodedata.combining(c))
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def make_fp(from_addr: str, subject: str) -> str:
    email = extract_email(from_addr) or ''
    subj  = _normalize_text(subject)
    key = f"{email}|{subj}"
    return hashlib.sha1(key.encode('utf-8')).hexdigest()

def salvage_llm_output(resp_text: str):
    """
    Essaie d'extraire summary/labels d'un JSON partiel/mal formé rendu par le LLM.
    Retourne (summary, labels_list) si trouvé, sinon (None, []).
    """
    if not resp_text:
        return None, []
    # 1) Cherche un bloc { ... } le plus long
    m = re.search(r'\{.*', resp_text, flags=re.S)  # commence à la première '{'
    if m:
        chunk = m.group(0)
        # tente un équilibrage simple des accolades
        open_b = chunk.count('{')
        close_b = chunk.count('}')
        if close_b < open_b:
            chunk = chunk + ('}' * (open_b - close_b))
        try:
            data = json.loads(chunk)
            summary = data.get("summary")
            labels = data.get("labels") or []
            if isinstance(labels, str):
                labels = [labels]
            return summary, labels
        except Exception:
            pass

    # 2) Regex tolérantes
    s = None
    m = re.search(r'"summary"\s*:\s*"([^"]+)"', resp_text, flags=re.S)
    if m:
        s = m.group(1).strip()

    labs = []
    m = re.search(r'"labels"\s*:\s*\[([^\]]*)\]', resp_text, flags=re.S)
    if m:
        raw = m.group(1)
        labs = re.findall(r'"([^"]+)"', raw)
    return s, labs

def _strip_html(html: str) -> str:
    html = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', ' ', html or '')
    return re.sub(r'<[^>]+>', ' ', html)

def extract_text_from_message(msg):
    """Retourne du texte brut; préfère text/plain, fallback text/html nettoyé."""
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            ctype = part.get_content_type()
            dispo = (part.get_content_disposition() or '').lower()
            if ctype == 'text/plain' and dispo != 'attachment':
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
        # fallback HTML
        for part in msg.walk():
            if part.get_content_type() == 'text/html':
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                try:
                    html = payload.decode(part.get_content_charset() or 'utf-8', errors='replace')
                except Exception:
                    html = payload.decode('utf-8', errors='replace')
                return _strip_html(html)
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
            text = _strip_html(text)
        return text

def has_attachments(msg):
    if not msg.is_multipart():
        return False
    for part in msg.walk():
        dispo = (part.get_content_disposition() or '').lower()
        if dispo == 'attachment':
            return True
    return False

# ----------------- DB -----------------

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
        auto_labels TEXT,
        fp TEXT
    )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON emails(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_from ON emails(from_addr)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_msgid ON emails(msg_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fp ON emails(fp)")

def ensure_fp_column_and_backfill(conn: sqlite3.Connection):
    cur = conn.cursor()
    cols = [r[1] for r in cur.execute("PRAGMA table_info(emails)").fetchall()]
    if "fp" not in cols:
        cur.execute("ALTER TABLE emails ADD COLUMN fp TEXT")
        conn.commit()
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fp ON emails(fp)")
    # backfill
    rows = cur.execute("SELECT id, from_addr, subject FROM emails WHERE fp IS NULL OR fp=''").fetchall()
    for i, (id_, frm, subj) in enumerate(rows, 1):
        cur.execute("UPDATE emails SET fp=? WHERE id=?", (make_fp(frm, subj), id_))
        if i % 1000 == 0:
            conn.commit()
    conn.commit()

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
    ensure_fp_column_and_backfill(conn)
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
        fp = make_fp(from_addr, subject)

        cur.execute("""
            INSERT OR IGNORE INTO emails
            (msg_id, from_addr, to_addr, cc_addr, bcc_addr, subject, date, ts, size_bytes, has_attachments, body, summary, auto_labels, fp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
        """, (msg_id, from_addr, to_addr, cc_addr, bcc_addr, subject, date_str, ts, size_bytes, attach, body, fp))
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
        return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")
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

def export_csv(db_path, out_path, encoding='utf-8'):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT date, from_addr, to_addr, subject, size_bytes, has_attachments, summary, auto_labels
        FROM emails
        ORDER BY ts DESC
    """)
    with open(out_path, 'w', newline='', encoding=encoding) as f:
        w = csv.writer(f)
        w.writerow(["date","from","to","subject","size_bytes","has_attachments","summary","auto_labels"])
        for row in rows:
            w.writerow(row)
    conn.close()
    print(f"Exported to {out_path} (encoding={encoding})")

def export_csv_bom(db_path, out_path):
    export_csv(db_path, out_path, encoding='utf-8-sig')  # Excel-friendly

# ----------------- Heuristiques -----------------

def heuristic_label(from_addr: str, subject: str, body: str = None):
    email = extract_email(from_addr) or ''
    dom = domain_of(email) or ''
    labels = []
    for rx, labs in DOMAIN_RULES:
        if re.search(rx, email) or re.search(rx, dom):
            for lb in labs:
                if lb not in labels:
                    labels.append(lb)
    s = _normalize_text(subject)
    for rx, labs in SUBJECT_RULES:
        if re.search(rx, s, flags=re.I):
            for lb in labs:
                if lb not in labels:
                    labels.append(lb)
    return ", ".join(labels[:3]) if labels else ""

def auto_label(db_path, limit=5000, only_empty=True):
    conn = sqlite3.connect(db_path)
    ensure_fp_column_and_backfill(conn)
    cur = conn.cursor()
    where = "WHERE auto_labels IS NULL OR auto_labels=''" if only_empty else ""
    rows = cur.execute(f"""
        SELECT id, from_addr, subject, fp FROM emails
        {where}
        ORDER BY ts DESC
        LIMIT ?
    """, (limit,)).fetchall()

    updated = 0
    for id_, frm, subj, fp in rows:
        labs_str = heuristic_label(frm, subj)
        if not labs_str:
            continue
        cur.execute("UPDATE emails SET auto_labels=? WHERE id=?", (labs_str, id_))
        # propage aux doublons (même fp) si non renseignés
        cur.execute("UPDATE emails SET auto_labels=COALESCE(auto_labels, ?) WHERE fp=?", (labs_str, fp))
        updated += 1

    conn.commit()
    conn.close()
    print(f"Auto-labeled (heuristics): {updated}")

# ----------------- Ollama -----------------

OLLAMA_URL = "http://localhost:11434/api/generate"

def run_ollama(model, prompt, options=None, timeout=45, force_json=True):
    """
    Appelle Ollama avec timeout par requête + retries.
    force_json=True demande une sortie JSON stricte (si modèle compatible).
    """
    payload = {"model": model, "prompt": prompt, "stream": False}
    if options:
        payload["options"] = options
    if force_json:
        payload["format"] = "json"
    tries = 2
    delay = 3
    last_exc = None
    for _ in range(tries):
        try:
            r = requests.post(OLLAMA_URL, json=payload, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            return data.get("response","").strip()
        except Exception as e:
            last_exc = e
            time.sleep(delay)
            delay *= 2
    raise last_exc

SUMMARY_PROMPT = """Tu es une IA qui résume des e-mails en français et propose des étiquettes de tri.
Voici l'e-mail (texte brut):

---
{body}
---

Objectif:
1) Résume en 1 phrase.
2) Propose 1 à 2 labels parmi: [Factures, Bancaire, Santé, Travail, École, RH, Rendez-vous, Technique, Newsletter, Promotions, Réseaux sociaux, Voyage, Livraison, Garanties, Notifications, Perso].
3) Indique si action nécessaire (oui/non) et une action courte.

Réponds strictement en JSON: {"summary": "...", "labels": ["..."], "action_needed": true/false, "action": "..."}
"""

def summarize_batch(db_path, model="mistral", limit=200, min_chars=200, dry=False,
                    clip=4000, num_predict=128, num_ctx=2048, temp=0.2,
                    out_jsonl=None, verbose=True, skip_duplicates=False):
    conn = sqlite3.connect(db_path)
    ensure_fp_column_and_backfill(conn)
    cur = conn.cursor()

    rows = cur.execute("""
        SELECT id, body, from_addr, subject, fp
        FROM emails
        WHERE (summary IS NULL OR summary = '')
          AND length(body) >= ?
        ORDER BY ts DESC
        LIMIT ?
    """, (min_chars, limit)).fetchall()

    # déduplication (From+Sujet)
    if skip_duplicates:
        seen = set()
        uniq = []
        for id_, body, frm, subj, fp in rows:
            fp = fp or make_fp(frm, subj)
            if fp in seen:
                continue
            seen.add(fp)
            uniq.append((id_, body, frm, subj, fp))
        rows = uniq
    else:
        rows = [(id_, body, frm, subj, fp or make_fp(frm, subj)) for id_, body, frm, subj, fp in rows]

    total_to_do = len(rows)
    print(f"To summarize: {total_to_do} emails")

    # warm-up
    try:
        _ = run_ollama(model, "Réponds: OK", options={"temperature": 0, "num_ctx": 512, "num_predict": 4}, timeout=20)
    except Exception as e:
        if verbose:
            print(f"[WARN] warm-up failed: {e}")

    jsonl = open(out_jsonl, "a", encoding="utf-8") if out_jsonl else None

    updated = 0
    start_time = time.time()

    for idx, (_id, body, from_addr, subject, fp) in enumerate(rows, 1):
        if verbose:
            print(f"\n[{idx}/{total_to_do}] ID={_id} | From: {from_addr} | Sujet: {subject[:60]!r} | fp={fp[:8]}")
        body_clip = (body or "")[:clip]
        prompt = SUMMARY_PROMPT.replace("{body}", body_clip)

        # Fallback par défaut (si IA échoue)
        labels_str = ""
        summary = ""

        try:
            resp = run_ollama(
                model, prompt,
                options={"temperature": float(temp), "num_ctx": int(num_ctx), "num_predict": int(num_predict)},
                timeout=45, force_json=True
            )
            parsed = None
            try:
                parsed = json.loads(resp)
            except Exception:
                m = re.search(r'\{.*\}', resp, flags=re.S)
                if m:
                    try:
                        parsed = json.loads(m.group(0))
                    except Exception:
                        parsed = None

            if parsed is None:
                # salvage malformed/partial JSON from LLM output
                salv_summary, salv_labels = salvage_llm_output(resp)
                if salv_summary or salv_labels:
                    summary = (salv_summary or "").strip()[:1000]
                    labels_list = salv_labels or []
                else:
                    # fallback pure heuristic
                    summary = (resp or "").strip()[:1000]
                    labels_list = []
            else:
                summary = (parsed.get("summary","") or "").strip()[:1000]
                labels_list = parsed.get("labels",[]) or []

            # fuse with heuristics if needed (heuristic_label returns a string)
            if not labels_list:
                labs_h = heuristic_label(from_addr, subject)
                if labs_h:
                    labels_list = [x.strip() for x in labs_h.split(',') if x.strip()]

            labels_str = ", ".join(labels_list[:5])

            # affichage détaillé
            if verbose:
                print(f"  ➜ Labels: {labels_str if labels_str else '(aucun)'}")
                if summary:
                    short = summary[:120] + ('...' if len(summary) > 120 else '')
                    print(f"  ➜ Résumé: {short}")
                else:
                    print("  ➜ Résumé: (vide)")

        except Exception as e:
            if verbose:
                print(f"[WARN] LLM fail for id={_id}: {e} -> fallback heuristics")
            labels_str = heuristic_label(from_addr, subject)
            summary = ""

        # write DB
        if not dry:
            cur.execute("UPDATE emails SET summary=?, auto_labels=? WHERE id=?", (summary, labels_str, _id))
            # propage aux doublons (même fp) si vides
            cur.execute("""
                UPDATE emails
                   SET summary    = COALESCE(summary, ?),
                       auto_labels= COALESCE(auto_labels, ?)
                 WHERE fp = ?
            """, (summary, labels_str, fp))
            updated += 1
            if updated % 20 == 0:
                conn.commit()
                if verbose:
                    elapsed = time.time() - start_time
                    avg_per = max(elapsed / updated, 1e-6)
                    remaining = (total_to_do - idx) * avg_per
                    print(f"  ➜ Progression: {updated}/{total_to_do} | ETA: {remaining/60:.1f} min")

        if jsonl:
            rec = {
                "id": _id,
                "from": from_addr,
                "subject": subject,
                "summary": summary,
                "labels": [x.strip() for x in labels_str.split(',')] if labels_str else [],
                "ts": int(time.time())
            }
            jsonl.write(json.dumps(rec, ensure_ascii=False) + "\n")
            jsonl.flush()

    conn.commit()
    conn.close()
    if jsonl:
        jsonl.close()
    print(f"\nDone. Updated {updated} emails.")

# ----------------- Count -----------------

def count_items(db_path, what: str):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    if what == "summarized":
        res = cur.execute("SELECT COUNT(*) FROM emails WHERE summary IS NOT NULL AND summary<>''").fetchone()[0]
    elif what == "labeled":
        res = cur.execute("SELECT COUNT(*) FROM emails WHERE auto_labels IS NOT NULL AND auto_labels<>''").fetchone()[0]
    elif what == "total":
        res = cur.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    elif what == "attachments":
        res = cur.execute("SELECT COUNT(*) FROM emails WHERE has_attachments=1").fetchone()[0]
    elif what == "unprocessed":
        res = cur.execute("SELECT COUNT(*) FROM emails WHERE (summary IS NULL OR summary='')").fetchone()[0]
    else:
        conn.close()
        raise SystemExit("Unknown count. Use summarized|labeled|total|attachments|unprocessed")
    conn.close()
    print(res)

# ----------------- CLI -----------------

def main():
    ap = argparse.ArgumentParser(description="Local Email AI Toolkit (full)")
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

    p_exp = sub.add_parser("export", help="Export CSV (UTF-8)")
    p_exp.add_argument("--db", required=True)
    p_exp.add_argument("--out", required=True)

    p_expx = sub.add_parser("export-excel", help="Export CSV (UTF-8 BOM, Excel-friendly)")
    p_expx.add_argument("--db", required=True)
    p_expx.add_argument("--out", required=True)

    p_aut = sub.add_parser("auto-label", help="Heuristic labeling by domain/subject")
    p_aut.add_argument("--db", required=True)
    p_aut.add_argument("--limit", type=int, default=5000)
    p_aut.add_argument("--all", action="store_true", help="Also overwrite rows that already have labels")

    p_sum = sub.add_parser("summarize", help="Summarize + auto-label via Ollama")
    p_sum.add_argument("--db", required=True)
    p_sum.add_argument("--model", default="mistral")
    p_sum.add_argument("--limit", type=int, default=200)
    p_sum.add_argument("--min-chars", type=int, default=200)
    p_sum.add_argument("--dry", action="store_true")
    p_sum.add_argument("--clip", type=int, default=4000, help="Max chars of email body to send to LLM")
    p_sum.add_argument("--num-predict", type=int, default=128, help="Max tokens to generate")
    p_sum.add_argument("--num-ctx", type=int, default=2048, help="Context window")
    p_sum.add_argument("--temp", type=float, default=0.2, help="Temperature")
    p_sum.add_argument("--out-jsonl", type=str, default=None, help="Write per-mail JSON lines to this file")
    p_sum.add_argument("--quiet", action="store_true", help="Less verbose logs")
    p_sum.add_argument("--skip-duplicates", action="store_true", help="Process only 1 mail per (from+subject) and propagate")

    p_cnt = sub.add_parser("count", help="Count items in DB")
    p_cnt.add_argument("--db", required=True)
    p_cnt.add_argument("--what", required=True, choices=["summarized","labeled","total","attachments","unprocessed"])

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
    elif args.cmd == "export-excel":
        export_csv_bom(args.db, args.out)
    elif args.cmd == "auto-label":
        auto_label(args.db, limit=args.limit, only_empty=(not args.all))
    elif args.cmd == "summarize":
        summarize_batch(
            args.db,
            model=args.model,
            limit=args.limit,
            min_chars=args.min_chars,
            dry=args.dry,
            clip=args.clip,
            num_predict=args.num_predict,
            num_ctx=args.num_ctx,
            temp=args.temp,
            out_jsonl=args.out_jsonl,
            verbose=not args.quiet,
            skip_duplicates=args.skip_duplicates
        )
    elif args.cmd == "count":
        count_items(args.db, args.what)

if __name__ == "__main__":
    main()

