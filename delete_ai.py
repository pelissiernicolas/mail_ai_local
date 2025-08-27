from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from pathlib import Path
import os, json

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

def get_service():
    token_path = Path("token.json")
    cred_path = Path("credentials.json")
    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception:
            creds = None

    # If no creds or scopes missing/invalid, run auth flow
    needs_auth = False
    if not creds:
        needs_auth = True
    else:
        # ensure required scope present
        sc = getattr(creds, "scopes", None)
        if not sc or not any(s in sc for s in SCOPES):
            needs_auth = True

    if needs_auth:
        if not cred_path.exists():
            raise SystemExit("Place credentials.json (OAuth client) next to the script and re-run to authorize deletion.")
        # remove stale token to force fresh consent
        if token_path.exists():
            try:
                token_path.unlink()
                print("[INFO] Old token.json removed to force re-auth with proper scopes.")
            except Exception:
                pass
        flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), SCOPES)
        try:
            creds = flow.run_local_server(port=0, open_browser=True)
        except Exception:
            print("[INFO] Fallback to console OAuth flow for deletion script")
            creds = flow.run_console()
        # save token
        token_path.write_text(creds.to_json(), encoding="utf-8")
        try:
            print(f"[INFO] New token scopes: {getattr(creds, 'scopes', None)}")
        except Exception:
            pass

    # refresh if needed
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            pass

    return build("gmail", "v1", credentials=creds)

def chunked(iterable, size=950):
    buf = []
    for x in iterable:
        buf.append(x)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf

def main():
    service = get_service()

    # 1. Cherche les mails avec le label _AI_DELETE
    results = service.users().messages().list(userId="me", q="label:_AI_DELETE").execute()
    messages = results.get("messages", [])
    all_ids = [m["id"] for m in messages]

    # Pagination (au cas où plus de 500 résultats)
    while "nextPageToken" in results:
        results = service.users().messages().list(userId="me", q="label:_AI_DELETE", pageToken=results["nextPageToken"]).execute()
        msgs = results.get("messages", [])
        all_ids.extend([m["id"] for m in msgs])

    print(f"Nombre de mails trouvés avec _AI_DELETE: {len(all_ids)}")

    # 2. Envoie-les à la corbeille (batch max 1000)
    for chunk in chunked(all_ids, 950):
        body = {"ids": chunk}
        try:
            service.users().messages().batchDelete(userId="me", body=body).execute()
            print(f"Supprimé {len(chunk)} mails...")
        except HttpError as e:
            # detect insufficient scopes
            msg = ""
            try:
                msg = e.content.decode() if hasattr(e, 'content') else str(e)
            except Exception:
                msg = str(e)
            if 'insufficientPermissions' in msg or 'Insufficient Permission' in msg:
                print("[ERROR] Le token OAuth n'a pas les scopes nécessaires (gmail.modify).")
                print("Supprimez token.json et relancez pour forcer l'authentification avec les bons scopes.")
                print("Ex: del token.json ; puis relancez: python delete_ai.py")
                return
            else:
                raise

    print("✅ Tous les mails _AI_DELETE ont été supprimés.")

if __name__ == "__main__":
    main()
