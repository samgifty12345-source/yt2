# YouTube Auto Uploader — Render Edition

Downloads videos from links in `videos.csv` and uploads them to your YouTube channel. Runs daily on Render as a Cron Job.

---

## How it works

1. You add video links to `videos.csv` and push to GitHub
2. Render runs `uploader.py` on your schedule
3. The script downloads each new video, uploads it to YouTube, and logs it in `done.txt` so it never re-uploads the same video

---

## One-time Setup

### Step 1 — Get your YouTube OAuth credentials

1. Go to https://console.cloud.google.com/
2. Create a project → enable YouTube Data API v3
3. Go to Credentials → Create → OAuth client ID → Desktop app
4. Download the JSON → save as client_secrets.json

### Step 2 — Get your Refresh Token (run this once locally)

  pip install google-auth-oauthlib
  python get_refresh_token.py

A browser will open. Log in with your YouTube account. Then it prints your tokens — copy them.

### Step 3 — Set env vars in Render

You already have these set — just make sure the values match:

  YOUTUBE_CLIENT_ID      → from Google Cloud
  YOUTUBE_CLIENT_SECRET  → from Google Cloud
  YOUTUBE_REFRESH_TOKEN  → from get_refresh_token.py output

### Step 4 — Set up Render Cron Job

1. In Render, create a new Cron Job
2. Connect your GitHub repo
3. Build Command:  pip install -r requirements.txt
4. Run Command:    python uploader.py
5. Schedule:       0 9 * * *   (daily at 9am UTC)

---

## Adding videos

Edit videos.csv and add a new row, then push to GitHub:

  url,title,notes,category_id,privacy
  https://youtube.com/shorts/abc123,My Title,my description,24,public

Already-uploaded videos are tracked in done.txt — won't re-upload.

---

## CSV columns

  url          → Full video link
  title        → Title for the upload
  notes        → Description
  category_id  → 24=Entertainment, 17=Sports, 20=Gaming, 22=People & Blogs
  privacy      → public / private / unlisted

---

## Cron schedule examples

  0 9 * * *    → Every day at 9am UTC
  0 9 * * 1    → Every Monday at 9am
  0 */6 * * *  → Every 6 hours
