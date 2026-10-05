# @Sport_sciencebot — setup guide

**What it does**

- **08:00 and 17:00 (Tashkent):** the bot takes a new article from the English sport-science sites in `sources.txt`. Gemini writes a full Uzbek version, and then checks it a second time for spelling, terminology and facts.
- The bot adds the article's video, GIF or photos, then sends the finished post **to you privately** with ✅ / ❌ buttons.
- **✅** → the post goes to the channel at **09:00 / 18:00**. If you tap ✅ later than that, it goes out within ~20 minutes.
- **❌** → the bot prepares a different article for you.
- **No answer for 5 hours** → nothing is posted.
- **Long texts** are cut with "..." and a "🔗 Batafsil" (read more) link to the source.

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
In 1–3 minutes a post arrives in your private chat with the bot. Tap **✅**, and it appears in the channel at the next check (≤ 20 min).
You can also send `/yangi` to the bot at any time to get an extra post.

---

## Changing things
- **Websites:** edit `sources.txt`.
- **Times:** `SLOTS = [9, 18]` in `bot.py`.
- **Check interval:** the `cron` line in `.github/workflows/post.yml`.

## Notes
- GitHub's timer can be a few minutes late. That's normal.
- The bot never repeats an article. It keeps its memory in `state.json`.
- Translations are checked twice by AI, but always read the preview before tapping ✅.
- Keep your bot token only in GitHub Secrets, never in the code. If it ever leaks, send `/revoke` to @BotFather to get a new one.
