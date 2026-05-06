# 🔥 Hyderabad Jobs API

A **lightweight, free-tier-optimised** job aggregation backend that fetches, filters, stores, and distributes Hyderabad walk-in & fresher jobs via the Adzuna API and Telegram.

Built with **Python FastAPI + SQLite** — no Redis, no heavy workers, no background processes.

---

## 📁 Project Structure

```
hyderabad-jobs-api/
├── app/
│   ├── main.py            # FastAPI application & startup
│   ├── config.py          # All env vars & business constants
│   ├── database.py        # Async SQLite handler (aiosqlite)
│   ├── routes/
│   │   ├── jobs.py        # GET /jobs, /walkins, /freshers, /stats
│   │   └── fetch.py       # GET /trigger-fetch (the main worker)
│   └── services/
│       ├── adzuna.py      # Adzuna API integration + filtering
│       └── telegram.py    # Telegram Bot notifications
├── requirements.txt
├── render.yaml            # Render.com deployment config
├── .env.example           # Template for environment variables
└── .gitignore
```

---

## 🚀 API Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Service info & endpoint list |
| `GET /health` | Health check (use with UptimeRobot) |
| `GET /jobs` | Latest 50 jobs |
| `GET /walkins` | Walk-in jobs only |
| `GET /freshers` | Fresher jobs only |
| `GET /stats` | Total jobs & today's count |
| `GET /trigger-fetch` | **Fetch from Adzuna + send to Telegram** |
| `GET /docs` | Interactive Swagger UI |

---

## ⚙️ Local Setup

### 1. Prerequisites

- Python 3.11+
- A free [Adzuna Developer account](https://developer.adzuna.com/)
- A Telegram bot (via [@BotFather](https://t.me/botfather))

### 2. Clone & Install

```bash
git clone <your-repo-url>
cd hyderabad-jobs-api

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### 3. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` and fill in your real values:

```env
APP_ID=your_adzuna_app_id
APP_KEY=your_adzuna_app_key
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id_or_channel
DB_DIR=.
```

> **How to get your Telegram CHAT_ID:**
> 1. Create a bot via `@BotFather` → copy the token
> 2. Send any message to your bot or add it to a channel
> 3. Open: `https://api.telegram.org/bot<TOKEN>/getUpdates`
> 4. Look for `"chat": {"id": -xxxxxxxxxx}` — that is your `CHAT_ID`
> For a **public channel**, use `@your_channel_name` as the `CHAT_ID`

### 4. Run Locally

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Visit:
- API: http://localhost:8000
- Swagger Docs: http://localhost:8000/docs

### 5. Test the Fetch

```bash
curl http://localhost:8000/trigger-fetch
```

---

## ☁️ Deploy on Render (Free Tier)

### Step 1 — Push to GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/yourusername/hyderabad-jobs-api.git
git push -u origin main
```

### Step 2 — Create Web Service on Render

1. Go to [render.com](https://render.com) → **New** → **Web Service**
2. Connect your GitHub repository
3. Render auto-detects `render.yaml` — confirm the settings:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - **Plan:** Free

### Step 3 — Set Environment Variables

In Render dashboard → your service → **Environment** tab, add:

| Key | Value |
|---|---|
| `APP_ID` | Your Adzuna app ID |
| `APP_KEY` | Your Adzuna app key |
| `TELEGRAM_BOT_TOKEN` | Your bot token |
| `TELEGRAM_CHAT_ID` | Your chat/channel ID |
| `DB_DIR` | `/tmp` |

### Step 4 — Deploy

Click **Deploy** — Render will build and start the service. Your URL will be:
```
https://hyderabad-jobs-api.onrender.com
```

---

## ⏰ Setup Free External Cron (REQUIRED)

Render free tier **auto-sleeps** after 15 min of inactivity. You need **two** external services:

### Service 1 — UptimeRobot (keeps server awake)

> Free at: https://uptimerobot.com

1. Sign up → **Add New Monitor**
2. Monitor type: **HTTP(s)**
3. URL: `https://your-app.onrender.com/health`
4. Monitoring interval: **5 minutes**
5. Save

This pings every 5 minutes to prevent Render from sleeping.

### Service 2 — cron-job.org (triggers job fetch)

> Free at: https://cron-job.org

1. Sign up → **Create Cronjob**
2. URL: `https://your-app.onrender.com/trigger-fetch`
3. Schedule: **Every 60 minutes**
4. Method: GET
5. Save

This calls the fetch endpoint every hour. The built-in cooldown inside `/trigger-fetch` prevents double-fetching even if called more frequently.

---

## 🔄 How It Works

```
cron-job.org (every 60 min)
       │
       ▼
GET /trigger-fetch
       │
       ├── Check 1-hour cooldown → skip if too soon
       │
       ├── Call Adzuna API (1 page, 50 results)
       │
       ├── Filter: must be Hyderabad + contain
       │   walkin / walk-in / interview / fresher
       │
       ├── Deduplicate against SQLite (by Adzuna ID)
       │
       ├── Insert new jobs into DB
       │
       ├── Cleanup: keep only latest 500 rows
       │
       ├── Filter for Telegram: only jobs created ≤ 48h ago
       │   (prevents spam after Render cold-start DB wipe)
       │
       └── Send new eligible jobs to Telegram channel
```

---

## 📲 Telegram Message Format

```
🔥 Hyderabad Job Update

🏢 Company: Infosys
🎯 Role: Walk-in Drive for Freshers - Hyderabad
📍 Location: Hyderabad, India
🔗 Apply: https://adzuna.com/jobs/...
```

---

## 🛡️ Anti-Spam Design (Cold Start Protection)

Render's free tier wipes the ephemeral filesystem on every cold start, which means the SQLite DB is **empty** after each wake-up. Without protection, the first `/trigger-fetch` call would re-post all fetched jobs to Telegram.

**Solution:** Jobs are only sent to Telegram if their `created_at` timestamp from Adzuna is within the last **48 hours** (configurable via `TELEGRAM_MAX_AGE_HOURS`). Older jobs are stored in the DB but **never re-posted**.

---

## 🔐 Environment Variables Reference

| Variable | Required | Description |
|---|---|---|
| `APP_ID` | ✅ | Adzuna API App ID |
| `APP_KEY` | ✅ | Adzuna API App Key |
| `TELEGRAM_BOT_TOKEN` | ✅ | Telegram Bot Token |
| `TELEGRAM_CHAT_ID` | ✅ | Telegram Chat/Channel ID |
| `DB_DIR` | Optional | Directory for `jobs.db` (default: `.`) |
| `FETCH_COOLDOWN_SECONDS` | Optional | Min seconds between fetches (default: `3600`) |
| `MAX_STORED_JOBS` | Optional | Max rows in DB (default: `500`) |
| `TELEGRAM_MAX_AGE_HOURS` | Optional | Max age for Telegram posts (default: `48`) |

---

## 📦 Dependencies

```
fastapi==0.111.1       — Web framework
uvicorn[standard]      — ASGI server
httpx                  — Async HTTP client (Adzuna + Telegram)
aiosqlite              — Async SQLite driver
python-dotenv          — .env file loader
```

Total install size: **~30 MB** — well within Render free tier limits.

---

## 🔮 Future Improvements

- Add `GET /jobs?category=bpo` query filtering
- Persist DB to a free external store (e.g., [Turso](https://turso.tech) — free LibSQL)
- Add a simple HTML job board at `/board`
- Weekly digest Telegram messages

---

## 📄 License

MIT — use freely, deploy anywhere.
