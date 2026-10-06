# @Sport_sciencebot — setup guide

**What it does**

- **Topic: gymnastics.** The bot covers gymnastics research (open-access studies from Europe PMC), gymnastics news (Inside Gymnastics, International Gymnast) and the gymnastics articles from general sport-science sites.
- **5 posts a day, exactly at the namaz times of Tashkent:** Bomdod, Peshin, Asr, Shom, Xufton. It uses the standard MWL calculation and fetches the times fresh each day.
- **1 hour before each namaz time**, the bot writes a full Uzbek post with photos, GIF or video and sends it **to you privately** with ✅ / ❌ buttons.
  - **✅** → published at the namaz time
  - **❌** → the bot prepares a different article
  - **no answer** → published **automatically** at the namaz time
- **Long texts** are cut with "..." and a "🔗 Batafsil" link to the source.
- Commands in Telegram: `/yangi` (extra post now), `/vaqtlar` (today's times), `/start`.

Everything runs free on GitHub.

---

## Setup (about 15 minutes)

### 1. Free Gemini key
Open https://aistudio.google.com/apikey → **Create API key** → copy it.

### 2. GitHub repository
1. Go to https://github.com/new and create a repository, for example `sport-science-bot`.
   **Public** is recommended because it gets unlimited free run time. Your token and keys stay hidden in Secrets either way.
2. Click **"uploading an existing file"** and drag in everything from this folder.
   Make sure the `.github/workflows/post.yml` file is uploaded too.
   If the browser skips the hidden `.github` folder, create it by hand: **Add file → Create new file**, name it `.github/workflows/post.yml`, and paste in its contents.

### 3. Secrets
In the repository, go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the token from BotFather |
| `TELEGRAM_CHANNEL` | your channel, e.g. `@my_channel` |
| `GEMINI_API_KEY` | the key from step 1 |

### 4. Your chat ID (so the bot knows who approves)
1. In Telegram, open **@Sport_sciencebot** and send `/start`.
2. On GitHub, open **Actions → Sport science bot → Run workflow**.
   If GitHub asks, first click "I understand my workflows, enable them".
3. Within ~1 minute the bot replies with **your chat ID**.
4. Add it as a fourth secret: `ADMIN_CHAT_ID`.

### 5. Test
Open **Actions → Run workflow**, tick **"Prepare a new post right now"**, and run it.
In 1–3 minutes a post arrives in your private chat with the bot. Tap **✅**, and it appears in the channel within seconds.
Easiest: send `/yangi` to the bot in Telegram at any time to get an extra post (no GitHub needed).

---

## Changing things
- **Websites:** edit `sources.txt`.
- **Times:** namaz times are automatic (`PRAYERS`, `PRAYER_METHOD` in `bot.py`).
- **Check interval:** the cron-job.org job (every 15 minutes).

## Notes
- cron-job.org starts a GitHub run every 15 min, 24 hours a day. Each run listens ~17 min, so buttons answer in seconds.
- Keep the repository **Public**: public repositories get unlimited free run time.
- The bot never repeats an article. It keeps its memory in `state.json`.
- Translations are checked twice by AI, but always read the preview before tapping ✅.
- Keep your bot token only in GitHub Secrets, never in the code. If it ever leaks, send `/revoke` to @BotFather to get a new one.
