"""
Run this once on your own computer (NOT on Railway) to get your
YOUTUBE_REFRESH_TOKEN.

Setup before running:
1. pip install google-auth-oauthlib
2. Put your downloaded client_secrets.json in this same folder
3. python get_refresh_token.py
"""

import json
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/drive.readonly",
]

try:
    with open("client_secrets.json", "r") as f:
        json.load(f)  # just validate it's real JSON before starting the flow
except FileNotFoundError:
    print("Error: 'client_secrets.json' file is missing. Download it from Google Cloud Console (Clients tab) and put it in this folder.")
    exit(1)
except json.JSONDecodeError:
    print("Error: 'client_secrets.json' is not valid JSON.")
    exit(1)

flow = InstalledAppFlow.from_client_secrets_file("client_secrets.json", SCOPES)
creds = flow.run_local_server(port=0)

print("\nSuccess! Copy these into your Railway environment variables:\n")
print(f"YOUTUBE_CLIENT_ID={creds.client_id}")
print(f"YOUTUBE_CLIENT_SECRET={creds.client_secret}")
print(f"YOUTUBE_REFRESH_TOKEN={creds.refresh_token}")
