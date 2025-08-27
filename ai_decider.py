#!/usr/bin/env python3
# ai_decider.py — IA decideur keep/archive/delete (script séparé)
# - Lit mail.db, ajoute colonnes si besoin (ai_decision/ai_reason/ai_conf)
# - Appelle Ollama (ex: model mistral) avec un prompt strict JSON
# - Ecrit en base + JSONL optionnel + export CSV (UTF-8 BOM)
#
# Exemples:
#   python ai_decider.py decide --db .\mail.db --model mistral --limit 500 --clip 1500 --num-predict 160 --num-ctx 2048 --temp 0.1 --out-jsonl ai_decisions.jsonl
#   python ai_decider.py preview --db .\mail.db
#   python ai_decider.py export --db .\mail.db --out .\decisions_ai.csv
#   (option) seuil de prudence: n'autoriser delete que si conf>=0.7: --min-conf-delete 0.7

import argparse, sqlite3, time, json, re, requests, csv, os

# Règles utilisateur -> priment sur la décision IA
USER_OVERRIDE_RULES = [
    # Quora digests
    {"from_rx": r"@quora\.com", "subj_rx": r".*", "decision": "delete", "reason": "rule: quora digest"},

    # Google security alerts
    {"from_rx": r"@accounts\.google\.com", "subj_rx": r"(alerte de s[ée]curit[ée]|security alert)", "decision": "delete", "reason": "rule: google security alert"},

    # PayPal new device
    {"from_rx": r"^service@paypal\.fr$", "subj_rx": r"(connexion depuis un nouvel appareil|new device|nouvel appareil)", "decision": "delete", "reason": "rule: paypal new device"},

    # 🔥 SPAM newsletters/promotions génériques → DELETE
    # Myfox -> spam pour toi
    {"from_rx": r"@myfox\.me|@myfox\.", "subj_rx": r".*", "decision": "delete", "reason": "rule: myfox spam"},

    # LinkedIn mails → spam
    {"from_rx": r"@linkedin\.com", "subj_rx": r".*", "decision": "delete", "reason": "rule: linkedin spam"},

    # Twitch account notifications
    {"from_rx": r"^account@twitch\.tv$", "subj_rx": r".*", "decision": "delete", "reason": "rule: twitch account"},

    # AliExpress transaction/notification emails
    {"from_rx": r"^transaction@notice\.aliexpress\.com$", "subj_rx": r".*", "decision": "delete", "reason": "rule: aliexpress transaction"},

    # domaines et patterns fréquents d'emailing
    {"from_rx": r"(newsletter|news\.|mailer|mailers?p\d+|email\.)", "subj_rx": r".*", "decision": "delete", "reason": "rule: generic newsletter sender"},
    # mots-clés promo dans l'objet (FR/EN)
    {"from_rx": r".*", "subj_rx": r"(newsletter|promo|promotion|ventes? priv[ée]es?|bon\s?plan|r[ée]duction|remise|offre|deal|soldes?|flash sale|discount|% off)", "decision": "delete", "reason": "rule: promo subject"},
]

OLLAMA_URL = "http://localhost:11434/api/generate"

DECIDE_PROMPT = """Tu es une IA qui classe les e-mails et décide quoi en faire.

Décisions possibles:
- "keep"    : garder en boîte de réception (utile / sensible / action proche)
- "archive" : retirer de la boîte (conserver dans Tous les messages)
- "delete"  : envoyer à la corbeille (contenu sans valeur ou redondant)

Politique (prudence mais efficace):
- Garde (keep) sécurité/bancaire/factures/RH/santé/confirmations récentes importantes.
- Archive newsletters/promotions récentes, et éléments non critiques mais possiblement utiles.
- Supprime (delete) promotions/newsletters anciennes, pubs, bruit technique redondant.

Décide uniquement à partir de l’émetteur, l’objet et le corps ci-dessous.
Sois concis, mais rends un JSON STRICT avec ces clés:

{"category": ["..."], "decision": "keep|archive|delete", "confidence": 0.0-1.0, "reason": "...", "summary": "..."}

From: {from_addr}
Subject: {subject}
---
{body}
---
"""

def ensure_ai_columns(conn):
    cur = conn.cursor()
    cols = [r[1] for r in cur.execute("PRAGMA table_info(emails)").fetchall()]
    changed = False
    if "ai_decision" not in cols:
        cur.execute("ALTER TABLE emails ADD COLUMN ai_decision TEXT")
        changed = True
    if "ai_reason" not in cols:
        cur.execute("ALTER TABLE emails ADD COLUMN ai_reason TEXT")
        changed = True
    if "ai_conf" not in cols:
        cur.execute("ALTER TABLE emails ADD COLUMN ai_conf REAL")
        changed = True
    if changed: conn.commit()

def run_ollama(model, prompt, options=None, timeout=45, force_json=True, tries=2):
    payload = {"model": model, "prompt": prompt, "stream": False}
    if options: payload["options"] = options
    if force_json: payload["format"] = "json"
    last = None
    delay = 3
    for _ in range(tries):
        try:
            r = requests.post(OLLAMA_URL, json=payload, timeout=timeout)
            r.raise_for_status()
            return r.json().get("response","").strip()
        except Exception as e:
            last = e; time.sleep(delay); delay *= 2
    raise last

def salvage_json(resp_text: str):
    """Tente d'extraire un JSON partiel { ... } et retourner dict ou None."""
    if not resp_text: return None
    # Essaie bloc { ... } + équilibrage simple des accolades
    m = re.search(r'\{.*', resp_text, flags=re.S)
    if m:
        chunk = m.group(0)
        ob = chunk.count('{'); cb = chunk.count('}')
        if cb < ob: chunk += '}' * (ob - cb)
        try:
            return json.loads(chunk)
        except Exception:
            pass
    # Regex tolérantes pour summary/labels/decision/confidence
    data = {}
    m = re.search(r'"decision"\s*:\s*"([^"]+)"', resp_text, flags=re.I|re.S)
    if m: data["decision"] = m.group(1).lower()
    m = re.search(r'"confidence"\s*:\s*([0-9.]+)', resp_text, flags=re.I|re.S)
    if m:
        try: data["confidence"] = float(m.group(1))
        except: pass
    m = re.search(r'"summary"\s*:\s*"([^"]+)"', resp_text, flags=re.I|re.S)
    if m: data["summary"] = m.group(1)
    m = re.search(r'"category"\s*:\s*\[([^\]]*)\]', resp_text, flags=re.I|re.S)
    if m:
        data["category"] = re.findall(r'"([^"]+)"', m.group(1))
    return data if data else None


def apply_user_overrides(from_addr: str, subject: str, curr_decision: str, category_text: str = ""):
    f = (from_addr or "").lower()
    s = (subject or "").lower()
    c = (category_text or "").lower()

    # 1) Si la catégorie IA mentionne newsletter/promotion → delete direct
    if "newsletter" in c or "promotion" in c or "promotions" in c:
        return "delete", "rule: ia category=promo/newsletter"

    # 2) Sinon, appliquer les règles regex from/subject
    for rule in USER_OVERRIDE_RULES:
        if re.search(rule["from_rx"], f) and re.search(rule["subj_rx"], s, flags=re.I):
            return rule["decision"], rule.get("reason", "override")
    return curr_decision, None

def decide_ai(db_path, model, limit, clip, num_predict, num_ctx, temp,
              out_jsonl=None, min_conf_delete=0.0, verbose=True):
    conn = sqlite3.connect(db_path)
    ensure_ai_columns(conn)
    cur = conn.cursor()

    rows = cur.execute("""
        SELECT id, from_addr, subject, body
        FROM emails
        WHERE ai_decision IS NULL OR ai_decision = ''
        ORDER BY ts DESC
        LIMIT ?
    """, (limit,)).fetchall()

    total = len(rows)
    print(f"To decide: {total} emails")
    jl = open(out_jsonl, "a", encoding="utf-8") if out_jsonl else None

    # warm-up
    try:
        _ = run_ollama(model, "Réponds: OK", options={"temperature":0,"num_ctx":512,"num_predict":4}, timeout=20)
    except Exception as e:
        if verbose: print(f"[WARN] warm-up failed: {e}")

    updated = 0
    for i, (eid, frm, sub, body) in enumerate(rows, 1):
        if verbose:
            print(f"\n[{i}/{total}] ID={eid} | From: {frm} | Subject: {str(sub or '')[:80]!r}")
        bclip = (body or "")[:clip]
        prompt = (DECIDE_PROMPT
                  .replace("{from_addr}", str(frm or ""))
                  .replace("{subject}", str(sub or ""))
                  .replace("{body}", bclip))

        decision, conf, reason, cat, summ = "archive", 0.5, "fallback", "", ""
        try:
            resp = run_ollama(model, prompt,
                              options={"temperature": float(temp), "num_ctx": int(num_ctx), "num_predict": int(num_predict)},
                              timeout=45, force_json=True)
            data = None
            try:
                data = json.loads(resp)
            except Exception:
                data = salvage_json(resp)

            if data:
                decision = (data.get("decision") or "archive").lower()
                if decision not in ("keep","archive","delete"):
                    decision = "archive"
                try: conf = float(data.get("confidence") or 0.5)
                except: conf = 0.5
                reason = (data.get("reason") or "")[:300]
                cat_list = data.get("category") or []
                if isinstance(cat_list, str): cat_list = [cat_list]
                cat = ", ".join([str(x) for x in cat_list])[:120]
                summ = (data.get("summary") or "")[:1000]

            # garde/abaisse delete si confiance trop basse
            if decision == "delete" and conf < float(min_conf_delete):
                decision = "archive"
                reason = (reason + f" | downgraded: conf<{min_conf_delete}").strip()

            # --- OVERRIDES utilisateur (priment sur l'IA) ---
            decision_before = decision
            decision, override_reason = apply_user_overrides(frm, sub, decision, category_text=cat)
            if override_reason:
                if decision_before != decision:
                    reason = (reason + f" | {override_reason}").strip()

            if verbose:
                print(f"  ➜ Decision: {decision} (conf={conf:.2f}) | Cat: {cat or '(none)'}")
                if summ: print(f"  ➜ Résumé: {summ[:120]}...")

            if summ:
                cur.execute("UPDATE emails SET ai_decision=?, ai_reason=?, ai_conf=?, auto_labels=COALESCE(auto_labels, ?), summary=COALESCE(summary, ?) WHERE id=?",
                            (decision, reason, conf, cat, summ, eid))
            else:
                cur.execute("UPDATE emails SET ai_decision=?, ai_reason=?, ai_conf=?, auto_labels=COALESCE(auto_labels, ?) WHERE id=?",
                            (decision, reason, conf, cat, eid))
            updated += 1
            if jl:
                jl.write(json.dumps({
                    "id": eid, "from": frm, "subject": sub,
                    "decision": decision, "confidence": conf,
                    "reason": reason, "category": cat, "summary": summ
                }, ensure_ascii=False) + "\n"); jl.flush()

            if updated % 25 == 0: conn.commit()

        except Exception as e:
            print(f"[WARN] id={eid}: {e}")

    conn.commit(); conn.close()
    if jl: jl.close()
    print(f"\nDone. AI-decided {updated} emails.")

def preview(db_path):
    conn = sqlite3.connect(db_path); c = conn.cursor()
    tot = c.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    rows = c.execute("""
        SELECT ai_decision, COUNT(*)
        FROM emails
        WHERE ai_decision IS NOT NULL AND ai_decision <> ''
        GROUP BY ai_decision
    """).fetchall()
    conn.close()
    print("=== Totaux ===")
    done = 0
    for d, n in rows:
        print(f"{d or '(vide)'}: {n}"); done += (n or 0)
    print(f"total decided: {done} / {tot}")

def export_csv(db_path, out_csv):
    conn = sqlite3.connect(db_path); c = conn.cursor()
    rows = c.execute("""
        SELECT msg_id, ai_decision, ai_conf, ai_reason, date, from_addr, subject
        FROM emails
        WHERE ai_decision IS NOT NULL AND ai_decision <> ''
        ORDER BY ts DESC
    """).fetchall()
    conn.close()
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["msg_id","action","confidence","reason","date","from","subject"])
        for r in rows: w.writerow(r)
    print(f"Exported -> {out_csv} (UTF-8 BOM)")

def main():
    ap = argparse.ArgumentParser(description="AI decider (keep/archive/delete) using Ollama")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("decide", help="Run AI decisions and save into DB")
    p1.add_argument("--db", required=True)
    p1.add_argument("--model", default="mistral")
    p1.add_argument("--limit", type=int, default=200)
    p1.add_argument("--clip", type=int, default=1500)
    p1.add_argument("--num-predict", type=int, default=160)
    p1.add_argument("--num-ctx", type=int, default=2048)
    p1.add_argument("--temp", type=float, default=0.1)
    p1.add_argument("--out-jsonl", type=str, default=None)
    p1.add_argument("--min-conf-delete", type=float, default=0.0, help="Seuil confiance pour accepter delete (sinon downgrade->archive)")

    p2 = sub.add_parser("preview", help="Show counts of AI decisions")
    p2.add_argument("--db", required=True)

    p3 = sub.add_parser("export", help="Export decisions to CSV (UTF-8 BOM)")
    p3.add_argument("--db", required=True)
    p3.add_argument("--out", default="decisions_ai.csv")

    p4 = sub.add_parser("overrides", help="Apply user override rules on already decided rows")
    p4.add_argument("--db", required=True)

    args = ap.parse_args()
    if args.cmd == "decide":
        decide_ai(args.db, args.model, args.limit, args.clip, args.num_predict, args.num_ctx, args.temp,
                  out_jsonl=args.out_jsonl, min_conf_delete=args.min_conf_delete, verbose=True)
    elif args.cmd == "preview":
        preview(args.db)
    elif args.cmd == "export":
        export_csv(args.db, args.out)
    elif args.cmd == "overrides":
        apply_overrides_on_db(args.db)

def apply_overrides_on_db(db_path):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    # inclure auto_labels/summary pour fournir un contexte de catégorie lors de la ré-application
    rows = c.execute("SELECT id, from_addr, subject, ai_decision, ai_reason, auto_labels, summary FROM emails WHERE ai_decision IS NOT NULL AND ai_decision<>''").fetchall()
    changed = 0
    for eid, frm, sub, dec, rsn, auto_lbls, summ in rows:
        # construire un petit texte de catégorie combinant auto_labels et summary
        cat_txt = ""
        if auto_lbls: cat_txt = str(auto_lbls)
        elif summ:
            # heuristique: chercher mots clefs promo/newsletter dans le summary
            cat_txt = summ[:200]
        new_dec, o_reason = apply_user_overrides(frm, sub, dec, category_text=cat_txt)
        if new_dec != dec:
            new_reason = (rsn or "")
            if o_reason:
                new_reason = (new_reason + " | " + o_reason).strip(" |")
            c.execute("UPDATE emails SET ai_decision=?, ai_reason=? WHERE id=?", (new_dec, new_reason, eid))
            changed += 1
    conn.commit(); conn.close()
    print(f"Overrides applied to {changed} emails.")

if __name__ == "__main__":
    main()
