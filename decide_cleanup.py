#!/usr/bin/env python3
# decide_cleanup.py — lit mail.db, calcule une catégorie et une action (keep/archive/delete)
# Sorties : decisions_preview.txt + decisions.csv (id,msg_id,date,from,subject,label,action,reason)

import sqlite3, re, datetime, csv, os

DB = r".\mail.db"
OUT_CSV = "decisions.csv"
OUT_PREVIEW = "decisions_preview.txt"

TODAY = datetime.datetime.utcnow().date()

# règles simples (tu peux ajuster)
KEEP_LABELS = {"Bancaire","Factures","Santé","Travail","École","RH","Livraison","Garanties","Perso"}
LOW_LABELS  = {"Promotions","Newsletter","Réseaux sociaux"}  # <- garde Réseaux sociaux comme “low"
SECURE_WORDS = re.compile(r"(security|sécurit|2fa|connexion|code|alerte|verif|unusual|nouvel appareil)", re.I)

# fenêtres de temps
KEEP_RECENT_DAYS = 14        # on garde tout ce qui a moins de 14 jours
LOW_TTL_DAYS     = 30        # newsletters/promo vieux de 30j -> archive/delete

# aide
def parse_date(d):
    if not d: return None
    try:
        # 2025-08-15T19:43:47+00:00
        return datetime.datetime.fromisoformat(d.replace("Z","+00:00")).date()
    except Exception:
        try:
            return datetime.datetime.strptime(d[:10], "%Y-%m-%d").date()
        except Exception:
            return None

def decide(row):
    """
    row: (id, msg_id, date, from_addr, subject, has_attachments, auto_labels, summary)
    retourne: action, reason, label_main
    """
    _id, msgid, d, frm, subj, attach, labels, summary = row
    date = parse_date(d)
    age = (TODAY - date).days if date else 9999
    label_set = {x.strip() for x in (labels or "").split(",") if x.strip()}
    label_main = next(iter(label_set), "")
    # normaliser casse/accents pour tests robustes
    label_norm = {x.strip().lower() for x in (labels or "").split(",") if x.strip()}

    # 0) tout récent -> garder
    if age <= KEEP_RECENT_DAYS:
        return "keep", f"recent<= {KEEP_RECENT_DAYS}j", label_main

    # 1) attachements -> garder par prudence (modifie si tu veux)
    if attach == 1:
        return "keep", "has_attachments", label_main

    text = f"{subj or ''} || {summary or ''}"

    # 2) sécurité -> garder
    if SECURE_WORDS.search(text):
        return "keep", "security_keywords", label_main

    # 3) labels forts -> garder
    if label_set & KEEP_LABELS:
        return "keep", "keep_label", label_main

    # 4) promos/newsletters -> SUPPRIMER systématiquement (sans condition d'âge)
    if {"promotions","newsletter"} & label_norm:
        return "delete", "promo/newsletter", label_main

    # 5) par défaut : archiver si vieux, garder sinon
    if age >= 90:
        return "archive", "old>=90d", label_main
    else:
        return "keep", "default", label_main

def main():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    rows = c.execute("""
        SELECT id, msg_id, date, from_addr, subject, has_attachments, auto_labels, summary
        FROM emails
        ORDER BY ts DESC
    """).fetchall()
    conn.close()

    stats = {"keep":0,"archive":0,"delete":0}
    by_label = {}
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id","msg_id","date","from","subject","label_main","action","reason"])
        for r in rows:
            action, reason, label_main = decide(r)
            stats[action]+=1
            by_label[label_main] = by_label.get(label_main,0)+1
            w.writerow([r[0], r[1], r[2], r[3], r[4], label_main, action, reason])

    with open(OUT_PREVIEW, "w", encoding="utf-8-sig") as f:
        f.write("=== Totaux par action ===\n")
        for k,v in stats.items():
            f.write(f"{k}: {v}\n")
        f.write("\n=== Top labels ===\n")
        for k in sorted(by_label, key=by_label.get, reverse=True)[:20]:
            f.write(f"{k or '(vide)'}: {by_label[k]}\n")

    print(f"OK -> {OUT_CSV} + {OUT_PREVIEW}")
    print("RAPPEL: c'est un APERÇU. Rien n'est supprimé/archivé dans Gmail tant que tu n'appliques pas l'étape 2.")
if __name__ == "__main__":
    main()
