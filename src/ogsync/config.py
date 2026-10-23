from pathlib import Path

CONFIG_DIR = Path.home() / ".config/ogsync"
# OAuth client (Desktop app) downloaded from Google Cloud Console.
CLIENT_SECRET = CONFIG_DIR / "client_secret.json"
GOOGLE_TOKEN = CONFIG_DIR / "google_token.json"
