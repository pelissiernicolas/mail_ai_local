#!/usr/bin/env python3
# apply_gmail_labels.py — crée les labels et range les mails selon ta base locale
# - Source: ai_decision + auto_labels
# - Action: ajoute des labels; optionnellement archive quand decision=archive
# - Jamais de delete ici (sécurisé)
#
# Prérequis:
#   pip install --upgrade google-api-python-client google-auth-httplib2 google-auth-oauthlib
#   credentials.json à côté du script (OAuth Desktop). Le script créera token.json au 1er run.
#
# Exemples:
#   DRY-RUN (par défaut):
#     python apply_gmail_labels.py --db .\mail.db
#   Exécuter réellement (sans suppression, avec archive pour decision=archive):
#     python apply_gmail_labels.py --db .\mail.db --no-dry-run --do-archive

import argparse, sqlite3, re, time, csv, sys, unicodedata
from pathlib import Path


def chunked(iterable, size=950):
    buf = []
    for x in iterable:
        buf.append(x)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# ————— Helpers Gmail —————

def get_service():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from pathlib import Path

    SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
    token = Path("token.json")
    credf = Path("credentials.json")

    creds = None
    if token.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token), SCOPES)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # refresh normal
            from google.auth.transport.requests import Request
            creds.refresh(Request())
        else:
            if not credf.exists():
                raise SystemExit("Place credentials.json dans ce dossier (OAuth Desktop).")
            flow = InstalledAppFlow.from_client_secrets_file(str(credf), SCOPES)
            # 1) tente serveur local (ouvre le navigateur et capture auto)
            try:
                creds = flow.run_local_server(port=0, open_browser=True)
            except Exception:
                # 2) fallback console: affiche l'URL, tu colles le code de validation dans le terminal
                print("[INFO] Fallback console OAuth flow (copier/coller le code d’autorisation)")
                creds = flow.run_console()

        token.write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds)

def fetch_labels_map(service):
    labs = service.users().labels().list(userId="me").execute().get("labels", [])
    return { L["name"]: L["id"] for L in labs }

def ensure_label(service, name, labels_map):
    original = name
    name = clean_label_name(name)
    if not name:
        if DEBUG_LABELS:
            print(f"[WARN] label ignoré (nom invalide) : {repr(original)}")
        return None
    if name in labels_map:
        return labels_map[name]
    body = {"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}
    try:
        created = service.users().labels().create(userId="me", body=body).execute()
        labels_map[name] = created["id"]
        if DEBUG_LABELS:
            print(f"[INFO] Label créé: {name}")
        return created["id"]
    except Exception as e:
        print(f"[ERROR] Échec création label {repr(name)} (depuis {repr(original)}): {e}")
        return None

def search_by_rfcid(service, rfc822msgid: str):
    # Recherche côté Gmail:
    q = f'rfc822msgid:{rfc822msgid}'
    resp = service.users().messages().list(userId="me", q=q, maxResults=1).execute()
    msgs = resp.get("messages", [])
    return msgs[0]["id"] if msgs else None

def batch_modify(service, ids, add=None, remove=None, dry_run=True):
    if not ids: return
    if dry_run:
        return
    body = {"ids": ids}
    if add: body["addLabelIds"] = add
    if remove: body["removeLabelIds"] = remove
    service.users().messages().batchModify(userId="me", body=body).execute()

# ————— Logique de labels —————

SAFE_LABELS = {
    # Normalisation: on garde des noms simples, consistants avec ce que tu utilises déjà
    "promotions": "Promotions",
    "newsletter": "Promotions",       # fusionne dans Promotions
    "bancaire": "Bancaire",
    "factures": "Factures",
    "finance": "Finance",
    "sécurité": "Sécurité",
    "securite": "Sécurité",
    "technique": "Technique",
    "travail": "Travail",
    "livraison": "Livraison",
    "commandes": "Commandes",
    "commandes/livraison": "Commandes/Livraison",
    "réseaux sociaux": "Réseaux sociaux",
    "reseaux sociaux": "Réseaux sociaux",
    "santé": "Santé",
    "ecole": "École",
    "école": "École",
    "perso": "Perso",
    "rh": "RH",
    "voyage": "Voyage",
    "notifications": "Notifications",
    "spam perso": "SpamPerso",
    "spamperso": "SpamPerso",
    "spam": "SpamPerso",
    "autres": "Autres",
    "rendez-vous": "Rendez-vous",
    "nextcloud": "Nextcloud",
    "sport": "Sport",
    "sports": "Sports",
}

def normalize_label(name: str) -> str:
    if not name: return "Autres"
    n = re.sub(r"\s+", " ", name.strip())
    key = n.lower()
    return SAFE_LABELS.get(key, n)  # par défaut, retourne le libellé tel quel (créé si absent)

DEBUG_LABELS = True  # passe à False quand tout est propre

def strip_control_chars(s: str) -> str:
    # supprime tous les caractères de contrôle (ASCII + Unicode)
    if not s:
        return ""
    return "".join(ch for ch in s
                   if unicodedata.category(ch)[0] != "C"  # Cc, Cf, etc.
                   or ch in ("\t", " ", "\n"))  # on re-nettoiera \t \n ensuite

def clean_label_name(name: str):
    if not name:
        return None
    s = str(name)

    # 1) supprime contrôles
    s = strip_control_chars(s)

    # 2) normalise espaces
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = re.sub(r"\s+", " ", s).strip()

    # 3) supprime guillemets/délimiteurs JSON probables
    s = s.strip(" '\"")

    # 4) interdit certains débuts / formes (labels système)
    if s.startswith("^"):   # labels système Gmail
        return None

    # 5) ignore ce qui ressemble à du JSON ou à une clé/valeur
    if any(tok in s for tok in ("{", "}", ":", '"')):
        return None

    # 6) pas de nom vide et limite raisonnable
    if not s:
        return None
    if len(s) > 200:
        s = s[:200]

    # 7) supprime slashs début/fin (nesting OK au milieu)
    s = s.lstrip("/").rstrip("/")

    # 8) cas pathologiques (string “None”, “(vide)”, …)
    if s.lower() in {"none", "null", "(vide)", "vide"}:
        return None

    return s or None

def parse_auto_labels(s: str):
    if not s:
        return []
    raw_parts = [p.strip() for p in s.split(",") if p.strip()]

    labs = []
    for part in raw_parts:
        # ignore morceaux manifestement issus d’un JSON/corps
        if any(tok in part for tok in ("{", "}", ":", '"')):
            continue
        # limite nombre de mots (éviter phrases complètes)
        if len(part.split()) > 4:
            continue
        # normalise via ton mapping + nettoyage final
        norm = normalize_label(part)
        clean = clean_label_name(norm)
        if clean:
            labs.append(clean)

    # dédoublonne en gardant l'ordre
    out, seen = [], set()
    for x in labs:
        if x not in seen:
            out.append(x); seen.add(x)
    return out[:5]

# ————— Lecture DB —————

def load_rows(db_path, only_actions=None, limit=None):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    q = """
        SELECT id, msg_id, from_addr, subject, ai_decision, ai_conf, ai_reason, auto_labels
        FROM emails
        WHERE msg_id IS NOT NULL AND msg_id <> ''
          AND ai_decision IS NOT NULL AND ai_decision <> ''
        ORDER BY ts DESC
    """
    rows = c.execute(q).fetchall()
    conn.close()
    if only_actions:
        rows = [r for r in rows if r[4] in only_actions]
    if limit:
        rows = rows[:limit]
    return rows

# ————— Programme principal —————

def main():
    ap = argparse.ArgumentParser(description="Créer labels Gmail et ranger les mails (sans delete).")
    ap.add_argument("--db", required=True)
    ap.add_argument("--limit", type=int, default=None, help="Limiter le nombre de messages à traiter")
    ap.add_argument("--only", choices=["keep","archive","delete","all"], default="all",
                    help="Quel sous-ensemble appliquer (delete est ignoré; ici on étiquette tout, archive seulement si decision=archive)")
    ap.add_argument("--no-dry-run", action="store_true", help="Exécuter réellement (par défaut: DRY-RUN)")
    ap.add_argument("--do-archive", action="store_true", help="Retirer INBOX pour les messages ai_decision=archive")
    ap.add_argument("--verbose", action="store_true", help="Afficher chaque message et ses actions")
    ap.add_argument("--log-csv", type=str, default=None, help="Journal des actions (CSV)")
    args = ap.parse_args()

    dry = not args.no_dry_run
    only_actions = None if args.only=="all" else [args.only]

    rows = load_rows(args.db, only_actions=only_actions, limit=args.limit)
    print(f"À traiter (rows): {len(rows)} | dry_run={dry} | do_archive={args.do_archive}")

    # optional CSV log
    logf = None; logw = None
    if args.log_csv:
        logf = open(args.log_csv, "w", newline="", encoding="utf-8-sig")
        logw = csv.writer(logf)
        logw.writerow(["msg_id","from","subject","ai_decision","auto_labels","gmail_id","labels_to_add","will_archive","dry_run"])

    svc = get_service()
    labels_map = fetch_labels_map(svc)
    inbox_id = labels_map.get("INBOX")  # pour removeLabelIds

    # Prépare un label pour marquage "classé via script" (optionnel)
    mark_name = "_CLASSÉ_PAR_SCRIPT"
    mark_id = ensure_label(svc, mark_name, labels_map)

    # Accumulation par label -> ids
    to_add_map = {}   # labelId -> [gmail_msg_ids...]
    to_mark_ids = []
    to_archive_ids = []

    BATCH_FIND = 500
    count_found = 0

    def add_for_label(lbl_name, gmail_id):
        lid = ensure_label(svc, lbl_name, labels_map)
        if not lid:
            return
        to_add_map.setdefault(lid, []).append(gmail_id)

    # Résout chaque msg_id via rfc822msgid, calcule labels à ajouter
    for (eid, rfcid, frm, subj, decision, conf, reason, auto_labs) in rows:
        # 1) retrouver l'ID Gmail par RFC822 Message-ID
        gid = None
        if rfcid:
            rid = rfcid.strip("<>")
            gid = search_by_rfcid(svc, rid)
        if not gid:
            continue  # introuvable (archivage déjà fait côté Gmail, ou message supprimé)

        count_found += 1
        # 2) labels depuis auto_labels
        labs = parse_auto_labels(auto_labs)

        # 3) Ajoute un label pour la décision (utile visuellement)
        decision_label = f"_AI_{decision.upper()}"
        labs.append(decision_label)

        # verbose output & CSV log
        if args.verbose:
            print(f"[MSG] {gid} | {frm} | {subj}")
            print(f"      decision={decision}  auto_labels={auto_labs}")

        # labels calculés (après parse_auto_labels)
        if args.verbose:
            print(f"      -> add_labels={labs}  {'(archive)' if args.do_archive and decision=='archive' else ''}")

        if logw:
            try:
                logw.writerow([rfcid, frm, subj, decision, auto_labs, gid, ";".join(labs), bool(args.do_archive and decision=='archive'), dry])
            except Exception:
                # évite de planter le script si écriture du CSV échoue
                pass

        # 4) Ajoute/accumule
        for L in labs:
            add_for_label(L, gid)

        # marquer que script est passé
        to_mark_ids.append(gid)

        # 5) Archive si decision=archive et demandé
        if args.do_archive and decision == "archive":
            to_archive_ids.append(gid)

        # petit throttle soft
        if count_found % BATCH_FIND == 0:
            time.sleep(0.2)

    # — Application —
    # a) Ajout des labels par lots (chunked <=950)
    for lid, ids in to_add_map.items():
        try:
            label_name = next((n for n, i in labels_map.items() if i == lid), str(lid))
        except Exception:
            label_name = str(lid)
        for chunk in chunked(ids, 950):
            print(f"Label + {len(chunk)} messages -> {label_name}")
            batch_modify(svc, chunk, add=[lid, mark_id], remove=None, dry_run=dry)

    # b) Ajoute le label de marquage aux autres (au cas où)
    rest_to_mark = [gid for gid in to_mark_ids if gid not in sum(to_add_map.values(), [])]
    if rest_to_mark:
        for chunk in chunked(rest_to_mark, 950):
            batch_modify(svc, chunk, add=[mark_id], remove=None, dry_run=dry)

    # c) Archive les "decision=archive" si demandé
    if args.do_archive and to_archive_ids:
        for chunk in chunked(to_archive_ids, 950):
            print(f"Archive (remove INBOX) -> {len(chunk)}")
            batch_modify(svc, chunk, add=None, remove=[inbox_id] if inbox_id else ["INBOX"], dry_run=dry)

    print("Terminé.")
    # close CSV log if ouvert
    if logf:
        try:
            logf.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()
