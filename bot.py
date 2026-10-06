"""
Gymnastics science Telegram bot
-------------------------------
English gymnastics news + gymnastics research  ->  full Uzbek post (Gemini, free)
->  photo / GIF / video  ->  preview sent to the OWNER with ✅ / ❌ buttons
->  published to the channel 5 times a day, exactly at the namaz times of Tashkent
    (Bomdod, Peshin, Asr, Shom, Xufton). No answer = published automatically.

Runs on GitHub Actions (free for public repositories), started every 15 minutes by cron-job.org.
Each run stays awake ~17 minutes, so button taps and /yangi are answered within seconds. It:
  1. reads button taps and commands sent to the bot,
  2. 1 hour before each namaz time prepares a draft and sends it to the owner,
  3. publishes the draft at the namaz time (✅ or no answer), ❌ = a different article.
"""
import html
import io
import json
import os
import re
import subprocess
import time
import traceback
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup
from PIL import Image

# ---------------- settings ----------------
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "").strip()       # @your_channel
ADMIN = os.environ.get("ADMIN_CHAT_ID", "").strip()            # your own Telegram chat ID
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODELS = list(dict.fromkeys(m for m in (
    os.environ.get("GEMINI_MODEL", "").strip(),
    "gemini-3.8-flash", "gemini-3.5-flash-lite",
    "gemini-flash-latest", "gemini-flash-lite-latest") if m))
FORCE_DRAFT = os.environ.get("FORCE_DRAFT", "").lower() == "true"
LISTEN_MINUTES = float(os.environ.get("LISTEN_MINUTES", "17"))   # how long each run stays awake

TZ = ZoneInfo("Asia/Tashkent")
LAT, LON = 41.2995, 69.2401              # Tashkent
PRAYER_METHOD = 3                        # 3 = Muslim World League (MWL)
PRAYERS = [("Fajr", "Bomdod"), ("Dhuhr", "Peshin"), ("Asr", "Asr"),
           ("Maghrib", "Shom"), ("Isha", "Xufton")]
DEFAULT_TIMES = {"Fajr": "05:00", "Dhuhr": "12:10", "Asr": "15:30",
                 "Maghrib": "18:00", "Isha": "19:30"}       # only if the times can't be fetched
PREPARE_BEFORE = timedelta(minutes=60)   # draft is sent to you 1 hour before
MIN_REVIEW = timedelta(minutes=15)       # a replacement draft (after ❌) gets at least 15 min
LATE_LIMIT = timedelta(hours=2)          # bot was offline: don't post more than 2 h late

# general sport-science feeds marked "[filter]" in sources.txt: keep only these topics
GYM_WORDS = re.compile(r"gymnast|trampolin|tumbling|acrobatic|pommel|balance beam|uneven bars|"
                       r"parallel bars|still rings|horizontal bar|rhythmic|cheerlead|"
                       r"calisthenic|handstand", re.I)

STATE_FILE = "state.json"
SOURCES_FILE = "sources.txt"
API = f"https://api.telegram.org/bot{TOKEN}"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

CAPTION_LIMIT = 1024        # Telegram limit for text under a photo/video
TEXT_LIMIT = 4096           # Telegram limit for a text-only message
MAX_FILE = 50 * 1024 * 1024
MAX_IMAGE_DOWNLOAD = 15 * 1024 * 1024
MAX_PHOTOS = 4


# ---------------- small helpers ----------------
def esc(s):
    return html.escape(s or "", quote=False)


def esc_attr(s):
    return html.escape(s or "", quote=True)


def clean(text):
    text = BeautifulSoup(text or "", "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def u16(s):
    """Telegram counts length in UTF-16 units (emoji = 2)."""
    return len(s.encode("utf-16-le")) // 2


def cut(text, limit):
    """Cut text to `limit` at a sentence end if possible. Returns (text, was_cut)."""
    if u16(text) <= limit:
        return text, False
    t = text[:max(limit, 0)]
    while t and u16(t) > limit:
        t = t[:-1]
    ends = [t.rfind(p) for p in (". ", "! ", "? ", ".\n", "\n")]
    best = max(ends)
    if best > len(t) * 0.5:
        t = t[:best + 1]
    else:
        sp = t.rfind(" ")
        if sp > 0:
            t = t[:sp]
    return t.rstrip(" ,;:-–—\n"), True


def now_tz():
    return datetime.now(TZ)


# ---------------- Telegram ----------------
def tg(method, data=None, files=None):
    r = requests.post(f"{API}/{method}", data=data, files=files, timeout=180)
    try:
        j = r.json()
    except ValueError:
        raise RuntimeError(f"{method}: HTTP {r.status_code}")
    if not j.get("ok"):
        raise RuntimeError(f"{method}: {j.get('description')}")
    return j["result"]


def tg_safe(method, data=None):
    try:
        return tg(method, data)
    except Exception as ex:
        print(f"[telegram] {ex}")
        return None


def say(chat, text):
    return tg_safe("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML"})


def set_button_text(d, text):
    """Replace the ✅/❌ message with a status line (buttons disappear)."""
    if d.get("button_id"):
        tg_safe("editMessageText", {"chat_id": ADMIN, "message_id": d["button_id"],
                                    "text": text, "parse_mode": "HTML"})


# ---------------- state ----------------
def load_state():
    s = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    s.setdefault("posted", [])
    s.setdefault("offset", 0)
    s.setdefault("drafts", {})
    s.setdefault("last_site", "")
    if os.path.exists("posted.json"):            # from the first version of the bot
        try:
            with open("posted.json", encoding="utf-8") as f:
                old = json.load(f).get("posted", [])
            s["posted"] = list(dict.fromkeys(old + s["posted"]))
        except Exception:
            pass
    return s


def save_state(s):
    s["posted"] = s["posted"][-2000:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


# ---------------- finding articles ----------------
def load_sources():
    with open(SOURCES_FILE, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]


def from_feed(url):
    r = requests.get(url, headers=UA, timeout=30)
    feed = feedparser.parse(r.content)
    items = []
    for e in feed.entries[:25]:
        if not e.get("link"):
            continue
        body = e.get("summary", "")
        if e.get("content"):
            body = e.content[0].get("value", body)
        media = []
        for m in (e.get("media_content") or []) + (e.get("media_thumbnail") or []):
            if m.get("url"):
                media.append(m["url"])
        for enc in e.get("enclosures") or []:
            if enc.get("href") and "image" in (enc.get("type") or ""):
                media.append(enc["href"])
        ts = e.get("published_parsed") or e.get("updated_parsed")
        items.append({
            "title": clean(e.get("title", "")),
            "link": e.link,
            "summary": clean(body)[:6000],
            "feed_media": media,
            "source": clean(feed.feed.get("title", "")) or urlparse(url).netloc,
            "time": time.mktime(ts) if ts else 0,
            "site": url,
        })
    return items


def from_europepmc(query):
    """Newest open-access research articles about gymnastics (Europe PMC, free API)."""
    r = requests.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                     params={"query": f"({query}) AND OPEN_ACCESS:y AND HAS_ABSTRACT:y",
                             "sort": "P_PDATE_D desc", "format": "json",
                             "resultType": "core", "pageSize": 25},
                     headers=UA, timeout=40)
    r.raise_for_status()
    items = []
    for a in r.json().get("resultList", {}).get("result", []):
        doi = a.get("doi")
        pmcid = a.get("pmcid")
        if doi:
            link = f"https://doi.org/{doi}"
        elif pmcid:
            link = f"https://europepmc.org/article/PMC/{pmcid}"
        else:
            link = f"https://europepmc.org/article/{a.get('source', 'MED')}/{a.get('id')}"
        try:
            ts = time.mktime(time.strptime(a.get("firstPublicationDate", "")[:10], "%Y-%m-%d"))
        except ValueError:
            ts = 0
        journal = (a.get("journalInfo") or {}).get("journal", {}).get("title") or "Europe PMC"
        items.append({"title": clean(a.get("title", "")).rstrip("."), "link": link,
                      "summary": clean(a.get("abstractText", ""))[:6000], "feed_media": [],
                      "source": journal, "time": ts, "site": "europepmc"})
    return items


def from_page(url):
    """For websites without RSS: take links that look like articles."""
    r = requests.get(url, headers=UA, timeout=30)
    soup = BeautifulSoup(r.text, "html.parser")
    host = urlparse(url).netloc
    items, seen = [], set()
    for a in soup.find_all("a", href=True):
        link = urljoin(url, a["href"]).split("#")[0]
        title = clean(a.get_text())
        if (urlparse(link).netloc == host and link not in seen and len(title) > 25
                and urlparse(link).path.count("-") >= 2):
            seen.add(link)
            items.append({"title": title, "link": link, "summary": "", "feed_media": [],
                          "source": host, "time": 0, "site": url})
    return items[:20]


def collect(exclude):
    items = []
    for line in load_sources():
        only_gym = line.lower().startswith("[filter]")
        url = line[8:].strip() if only_gym else line
        try:
            if url.lower().startswith("europepmc:"):
                found = from_europepmc(url.split(":", 1)[1].strip())
            else:
                found = from_feed(url) or from_page(url)
            if only_gym:
                found = [i for i in found if GYM_WORDS.search(i["title"] + " " + i["summary"])]
            print(f"[source] {url}: {len(found)} articles")
            items += [i for i in found if i["link"] not in exclude]
        except Exception as ex:
            print(f"[skip source] {url}: {ex}")
    return items


def rank(items, last_site):
    """Newest first, a different website than last time, Gemini picks the most interesting."""
    items = sorted(items, key=lambda i: i["time"], reverse=True)
    pool = [i for i in items if i["site"] != last_site] or items
    per_site, balanced = {}, []                 # up to 5 newest from each website
    for i in pool:
        if per_site.get(i["site"], 0) < 5:
            per_site[i["site"]] = per_site.get(i["site"], 0) + 1
            balanced.append(i)
    pool = balanced[:20]
    if GEMINI_KEY and len(pool) > 1:
        listing = "\n".join(f"{n}. {i['title']}" for n, i in enumerate(pool, 1))
        prompt = (
            "You choose articles for a popular Uzbek Telegram channel about GYMNASTICS science and "
            "gymnastics sport (artistic, rhythmic, trampoline, acrobatic, aerobic gymnastics) for "
            "coaches, gymnasts, parents, PE teachers and students. From the list below pick the ONE "
            "article that is most interesting and useful for them: training methods, technique and "
            "biomechanics, flexibility, strength, injuries and prevention, children and youth "
            "gymnasts, psychology, nutrition, judging, technology and analytics in gymnastics, or "
            "important gymnastics news (World Championships, Olympics, famous gymnasts). Articles "
            "that are not about gymnastics are the last choice. Avoid dry methodological papers "
            "(bibliometric analyses, protocols, questionnaire validation).\n"
            f"Answer with the number only.\n\n{listing}")
        try:
            n = int(re.search(r"\d+", gemini(prompt, 0.2, lite_first=True)).group())
            if 1 <= n <= len(pool):
                chosen = pool.pop(n - 1)
                pool.insert(0, chosen)
        except Exception as ex:
            print(f"[rank] {ex}")
    return pool


# ---------------- reading the article ----------------
BAD_IMG = re.compile(r"logo|icon|avatar|sprite|badge|pixel|placeholder|blank|spinner|gravatar|"
                     r"emoji|share|social|advert|/ads?/|banner|author|profile", re.I)
EMBED = re.compile(r"(youtube(?:-nocookie)?\.com/embed/|youtu\.be/|youtube\.com/watch|"
                   r"player\.vimeo\.com/video/)", re.I)


def main_container(soup):
    for sel in ("[itemprop=articleBody]", "article", "main", ".entry-content",
                ".article-body", "#text", ".post-content"):
        el = soup.select_one(sel)
        if el and len(el.find_all("p")) >= 2:
            return el
    best = max(soup.find_all("div"), key=lambda d: len(d.find_all("p", recursive=False)),
               default=None)
    return best or soup.body or soup


def watch_url(u):
    m = re.search(r"(?:embed/|youtu\.be/|v=)([\w-]{6,})", u)
    if "youtu" in u and m:
        return f"https://www.youtube.com/watch?v={m.group(1)}"
    m = re.search(r"vimeo\.com/video/(\d+)", u)
    if m:
        return f"https://vimeo.com/{m.group(1)}"
    return u


def img_src(img):
    srcset = img.get("srcset") or img.get("data-srcset")
    if srcset:
        last = srcset.split(",")[-1].strip().split(" ")[0]
        if last and not last.startswith("data:"):
            return last
    for attr in ("data-src", "data-lazy-src", "data-original", "src"):
        v = img.get(attr)
        if v and not v.startswith("data:"):
            return v
    return None


def find_media(soup, base):
    media = {"video": None, "embed": None, "images": []}

    def absu(u):
        return urljoin(base, u.strip()) if u else None

    for prop in ("og:video:secure_url", "og:video:url", "og:video"):
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            u = absu(tag["content"])
            if EMBED.search(u):
                media["embed"] = media["embed"] or watch_url(u)
            elif re.search(r"\.mp4(\?|$)", u, re.I):
                media["video"] = media["video"] or u

    box = main_container(soup)
    for v in box.find_all("video"):
        src = v.get("src") or (v.find("source") or {}).get("src")
        if src and not media["video"]:
            media["video"] = absu(src)
    for f in box.find_all("iframe"):
        src = f.get("src") or f.get("data-src") or ""
        if EMBED.search(src) and not media["embed"]:
            media["embed"] = watch_url(absu(src))

    images = []
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        images.append(absu(og["content"]))
    for img in box.find_all("img"):
        src = img_src(img)
        if not src:
            continue
        try:
            if int(img.get("width", 999)) < 150:
                continue
        except ValueError:
            pass
        u = absu(src)
        if BAD_IMG.search(u) or u.lower().split("?")[0].endswith(".svg"):
            continue
        images.append(u)
    media["images"] = list(dict.fromkeys(images))[:10]
    return media


def extract_text(soup):
    box = main_container(soup)
    for t in box(["script", "style", "nav", "footer", "header", "aside", "form",
                  "button", "noscript"]):
        t.decompose()
    parts = []
    for el in box.find_all(["h2", "h3", "p", "li"]):
        t = clean(el.get_text(" "))
        if len(t) > 30 or (el.name in ("h2", "h3") and len(t) > 3):
            parts.append(t)
    text = "\n".join(dict.fromkeys(parts))
    # stop before reference lists
    m = re.search(r"\n(References|Reference list|Bibliography)\n", text)
    return text[:m.start()] if m else text


def fetch_article(item):
    text, media = "", {"video": None, "embed": None, "images": []}
    try:
        r = requests.get(item["link"], headers=UA, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        media = find_media(soup, r.url)
        text = extract_text(soup)
    except Exception as ex:
        print(f"[article] {item['link']}: {ex}")
    if len(text) < 400 and len(item.get("summary", "")) > len(text):
        text = item["summary"]
    media["images"] = list(dict.fromkeys(media["images"] + item.get("feed_media", [])))
    return text[:14000], media


# ---------------- media download ----------------
def download(url, max_bytes):
    with requests.get(url, headers=UA, timeout=60, stream=True) as r:
        r.raise_for_status()
        if int(r.headers.get("content-length") or 0) > max_bytes:
            raise ValueError("file too big")
        buf = io.BytesIO()
        for chunk in r.iter_content(65536):
            buf.write(chunk)
            if buf.tell() > max_bytes:
                raise ValueError("file too big")
        return buf.getvalue()


def prepare_image(url):
    """Returns ("gif", bytes) / ("photo", jpeg bytes) / None if too small or broken."""
    data = download(url, MAX_IMAGE_DOWNLOAD)
    im = Image.open(io.BytesIO(data))
    w, h = im.size
    if w < 300 or h < 200 or max(w, h) / min(w, h) > 8:
        return None
    if im.format == "GIF" and getattr(im, "is_animated", False):
        return ("gif", data)
    im = im.convert("RGB")
    im.thumbnail((2560, 2560))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=88)
    return ("photo", out.getvalue())


def prepare_media(media):
    plan = {"video": None, "gif": None, "photos": [], "embed": media.get("embed")}
    if media.get("video"):
        try:
            plan["video"] = download(media["video"], MAX_FILE)
        except Exception as ex:
            print(f"[video] {ex}")
    for u in media.get("images", []):
        if len(plan["photos"]) >= MAX_PHOTOS:
            break
        try:
            res = prepare_image(u)
        except Exception as ex:
            print(f"[image] {u}: {ex}")
            continue
        if not res:
            continue
        kind, data = res
        if kind == "gif":
            if not plan["gif"]:
                plan["gif"] = data
        else:
            plan["photos"].append(data)
    return plan


# ---------------- Gemini (free) ----------------
def gemini_error(r):
    try:
        e = r.json().get("error", {})
        return f"HTTP {r.status_code} {e.get('status', '')}: {e.get('message', '')[:700]}"
    except Exception:
        return f"HTTP {r.status_code}: {r.text[:300]}"


USED_UP = set()        # models whose daily free quota is finished (during this run)


def gemini(prompt, temperature=0.4, lite_first=False):
    models = GEMINI_MODELS
    if lite_first:
        models = sorted(models, key=lambda m: "lite" not in m)
    errors = []
    for model in models:
        if model in USED_UP:
            continue
        for attempt in range(3):
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": GEMINI_KEY},
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {"temperature": temperature}},
                    timeout=180)
            except Exception as ex:
                errors.append(f"{model}: {ex}")
                print(f"[gemini] {model}: {ex}")
                time.sleep(5)
                continue
            if r.ok:
                try:
                    parts = r.json()["candidates"][0]["content"]["parts"]
                    out = "".join(p.get("text", "") for p in parts).strip()
                    if out:
                        print(f"[gemini] {model}: OK")
                        return out
                    msg = "empty answer"
                except Exception:
                    msg = f"unexpected answer: {r.text[:300]}"
            else:
                msg = gemini_error(r)
            errors.append(f"{model}: {msg}")
            print(f"[gemini] {model}: {msg}")
            if r.status_code == 429 and ("limit: 0" in msg or re.search(r"retry in \d+h", msg)
                                         or "per day" in msg.lower() or "PerDay" in msg):
                USED_UP.add(model)                  # daily quota finished -> next model now
                break
            if r.status_code in (500, 503) or (r.status_code == 429 and attempt == 0):
                time.sleep(30)                      # busy / per-minute limit -> wait and retry
                continue
            break                                   # no access / not found -> next model
    raise RuntimeError("Gemini failed:\n  " + "\n  ".join(errors))


WRITE_PROMPT = """You are the editor of an Uzbek-language Telegram channel about gymnastics science and gymnastics sport (sport gimnastikasi, badiiy gimnastika, batut, akrobatika) for coaches, gymnasts, parents, PE teachers and students.

Task: using ONLY the English article below, write a complete Uzbek version of it for the channel.

Requirements:
- Literary Uzbek in the Latin script with correct modern spelling (use o‘ g‘ and ʼ, e.g. "o‘quv", "mashg‘ulot", "ta’sir" -> "taʼsir").
- Keep ALL substantive information: main findings, numbers, percentages, units, number of participants, durations, names of studies, universities, organizations, technologies and tools. Do not leave out anything important. Drop menus, ads, author bios and reference lists.
- Do NOT add any fact, number, name or claim that is not in the article.
- Use correct Uzbek gymnastics and sport-science terminology (e.g. artistic gymnastics = sport gimnastikasi, rhythmic gymnastics = badiiy gimnastika, trampoline = batut, acrobatic gymnastics = sport akrobatikasi, floor exercise = erkin mashqlar, apparatus = snaryad, routine = mashq/kombinatsiya, coach = murabbiy, gymnast = gimnastikachi). For apparatus names (balance beam, vault, uneven bars, pommel horse, still rings, parallel bars) use the established Uzbek name if you are sure of it, and always add the English name in parentheses on first use. If a term has no established Uzbek equivalent, write the Uzbek term and give the English term in parentheses on first use, e.g. "maksimal kislorod isteʼmoli (VO2max)".
- Translate the meaning naturally, not word for word. Short clear paragraphs. You may end with "Amaliy xulosalar:" and 2-4 lines starting with "•" if the article supports them.
- Plain text only: no markdown, no asterisks, no hashtags, no links.
- At most 2500 characters.

Output exactly in this format and nothing else:
SARLAVHA: <short catchy Uzbek headline starting with one fitting emoji>
MATN:
<the Uzbek text>

Article title: {title}
Article text:
{text}
"""

CHECK_PROMPT = """You are a professional Uzbek editor and English-Uzbek translator specialised in gymnastics and sport science.
Below are an English source article and its Uzbek version for a Telegram channel.
Carefully check the Uzbek version and correct:
- spelling and grammar mistakes (Latin script, o‘ g‘ ʼ),
- unnatural or calqued wording,
- wrong sport-science terminology,
- mistranslations and any number, name or fact that does not match the English source.
Remove anything not supported by the source. Do not shorten the text otherwise.
Return the corrected version in exactly the same format (SARLAVHA: / MATN:) and nothing else.

ENGLISH SOURCE:
Title: {title}
{text}

UZBEK VERSION:
{draft}
"""


def parse_post(out):
    out = out.replace("**", "").replace("##", "").strip()
    head = re.search(r"SARLAVHA:\s*(.+)", out)
    body = re.search(r"MATN:\s*(.*)", out, re.S)
    if head and body:
        return head.group(1).strip(), body.group(1).strip()
    lines = [l for l in out.splitlines() if l.strip()]
    return lines[0].strip(), "\n".join(lines[1:]).strip()


def write_post(item, text):
    if not GEMINI_KEY:
        return f"🏃 {item['title']}", text[:2000]
    src = text[:12000]
    draft = gemini(WRITE_PROMPT.format(title=item["title"], text=src))
    try:
        checked = gemini(CHECK_PROMPT.format(title=item["title"], text=src, draft=draft), 0.2)
        if "MATN:" in checked:
            draft = checked
    except Exception as ex:
        print(f"[proofread skipped] {ex}")
    return parse_post(draft)


# ---------------- building and sending a post ----------------
def build_html(headline, body, link, source, video_link, limit):
    footer_plain = "\n\n🔗 Batafsil: " + source
    video_plain = "\n\n▶️ Videoni ko‘rish" if video_link else ""
    room = limit - u16(headline + "\n\n" + video_plain + footer_plain) - 3
    body_cut, was_cut = cut(body, room)
    if was_cut:
        body_cut = body_cut.rstrip(".!?…") + "..."
    out = f"<b>{esc(headline)}</b>\n\n{esc(body_cut)}"
    if video_link:
        out += f'\n\n▶️ <a href="{esc_attr(video_link)}">Videoni ko‘rish</a>'
    out += f'\n\n🔗 Batafsil: <a href="{esc_attr(link)}">{esc(source)}</a>'
    return out


def send_post(chat, caption, long_text, plan, preview_url):
    """Sends the post to `chat`. Returns list of message ids."""
    base = {"chat_id": chat, "parse_mode": "HTML"}
    try:
        if plan["video"]:
            r = tg("sendVideo", {**base, "caption": caption, "supports_streaming": "true"},
                   {"video": ("video.mp4", plan["video"], "video/mp4")})
            return [r["message_id"]]
    except Exception as ex:
        print(f"[send video] {ex}")
    try:
        if plan["gif"]:
            r = tg("sendAnimation", {**base, "caption": caption},
                   {"animation": ("animation.gif", plan["gif"], "image/gif")})
            return [r["message_id"]]
    except Exception as ex:
        print(f"[send gif] {ex}")
    photos = plan["photos"]
    try:
        if len(photos) == 1:
            r = tg("sendPhoto", {**base, "caption": caption},
                   {"photo": ("photo.jpg", photos[0], "image/jpeg")})
            return [r["message_id"]]
        if len(photos) > 1:
            media, files = [], {}
            for i, data in enumerate(photos):
                entry = {"type": "photo", "media": f"attach://p{i}"}
                if i == 0:
                    entry.update(caption=caption, parse_mode="HTML")
                media.append(entry)
                files[f"p{i}"] = (f"p{i}.jpg", data, "image/jpeg")
            r = tg("sendMediaGroup", {"chat_id": chat, "media": json.dumps(media)}, files)
            return [m["message_id"] for m in r]
    except Exception as ex:
        print(f"[send photos] {ex}")
    r = tg("sendMessage", {**base, "text": long_text, "link_preview_options": json.dumps(
        {"url": preview_url, "prefer_large_media": True, "show_above_text": True})})
    return [r["message_id"]]


def make_draft(state, key, publish_at, label, auto=False):
    exclude = set(state["posted"]) | {d.get("link") for d in state["drafts"].values()}
    items = collect(exclude)
    if not items:
        return None
    for item in rank(items, state["last_site"])[:4]:
        try:
            text, media = fetch_article(item)
            if len(text) < 200:
                continue
            headline, body = write_post(item, text)
            plan = prepare_media(media)
            has_media = plan["video"] or plan["gif"] or plan["photos"]
            caption = build_html(headline, body, item["link"], item["source"],
                                 plan["embed"], CAPTION_LIMIT)
            long_text = build_html(headline, body, item["link"], item["source"],
                                   plan["embed"], TEXT_LIMIT)
            ids = send_post(ADMIN, caption, long_text, plan,
                            plan["embed"] or item["link"])
            kind = ("video" if plan["video"] else "GIF" if plan["gif"] else
                    f"{len(plan['photos'])} ta rasm" if plan["photos"] else "faqat matn")
            buttons = {"inline_keyboard": [[
                {"text": "✅ Chop etish", "callback_data": f"ok|{key}"},
                {"text": "❌ O‘tkazib yuborish", "callback_data": f"no|{key}"}]]}
            btn = tg("sendMessage", {
                "chat_id": ADMIN, "parse_mode": "HTML",
                "text": (f"📝 <b>{esc(label)}</b> uchun post tayyor ({kind}).\n"
                         f"Manba: {esc(item['title'])}\n\n"
                         + (f"✅ — {publish_at.strftime('%H:%M')} da chiqadi\n"
                            f"❌ — boshqa maqola tayyorlanadi\n"
                            f"Javob bermasangiz, {publish_at.strftime('%H:%M')} da "
                            f"avtomatik chiqadi." if auto else "Kanalga chiqarilsinmi?")),
                "reply_markup": json.dumps(buttons),
                "link_preview_options": json.dumps({"is_disabled": True})})
            return {"status": "pending", "link": item["link"], "site": item["site"],
                    "title": item["title"], "preview_ids": ids, "button_id": btn["message_id"],
                    "publish_at": publish_at.isoformat(), "label": label, "auto": auto,
                    "created": now_tz().isoformat(), "media": bool(has_media)}
        except RuntimeError as ex:
            if str(ex).startswith("Gemini failed"):
                print(ex)
                say(ADMIN, "⚠️ Gemini ishlamadi, post tayyorlanmadi:\n<code>"
                           + esc(str(ex))[:3000] + "</code>")
                return False
            traceback.print_exc()
        except Exception:
            traceback.print_exc()
    return None


def publish(state, d, auto=False):
    try:
        tg("copyMessages", {"chat_id": CHANNEL, "from_chat_id": ADMIN,
                            "message_ids": json.dumps(d["preview_ids"])})
    except Exception as ex:
        print(f"[publish] {ex}")
        if not d.get("publish_error"):
            d["publish_error"] = str(ex)
            say(ADMIN, "⚠️ Kanalga chiqarib bo‘lmadi:\n<code>" + esc(str(ex)) + "</code>\n"
                       "Bot kanalda admin ekanini va «Post messages» ruxsati borligini tekshiring.")
        return
    d["status"] = "published"
    state["posted"].append(d["link"])
    state["last_site"] = d["site"]
    set_button_text(d, ("⏰ Javob bo‘lmadi — avtomatik chop etildi.\n" if auto else "")
                    + f"📢 Kanalga chiqdi: {esc(d['title'])}")
    print(f"Published: {d['title']}")


# ---------------- reading your button taps and commands ----------------
def process_updates(state, wait=0):
    """Reads new taps/commands (waits up to `wait` seconds for them).
    Returns True if the owner asked for a new post right now (/yangi)."""
    want_now = False
    updates = tg("getUpdates", {"offset": state["offset"], "timeout": wait,
                                "allowed_updates": json.dumps(["message", "callback_query"])})
    for u in updates:
        state["offset"] = u["update_id"] + 1
        msg = u.get("message")
        if msg and msg.get("text"):
            chat = str(msg["chat"]["id"])
            cmd = msg["text"].strip().split()[0].lower()
            print(f"[message] {chat}: {cmd}")
            if not ADMIN and cmd in ("/start", "/id"):
                say(chat, f"Sizning chat ID raqamingiz: <code>{chat}</code>\n"
                          "Uni GitHub'da <b>ADMIN_CHAT_ID</b> secret sifatida saqlang.")
            elif chat == ADMIN and cmd in ("/start", "/id"):
                times = ", ".join(f"{uz} {t}" for uz, t in today_times(state, now_tz()))
                say(chat, "✅ Bot ishlayapti.\nKuniga 5 ta post, namoz vaqtlarida chiqadi:\n"
                          f"{times}\n\nHar bir post 1 soat oldin sizga yuboriladi. "
                          "✅ — vaqtida chiqadi, ❌ — boshqa maqola, javob bo‘lmasa — "
                          "avtomatik chiqadi.\n/yangi — hoziroq qo‘shimcha post tayyorlash.\n"
                          "/vaqtlar — bugungi namoz vaqtlari.")
            elif chat == ADMIN and cmd == "/vaqtlar":
                times = "\n".join(f"• {uz}: {t}" for uz, t in today_times(state, now_tz()))
                say(chat, f"🕌 Bugungi post vaqtlari (Toshkent):\n{times}")
            elif chat == ADMIN and cmd == "/yangi":
                say(chat, "⏳ Yangi post tayyorlanmoqda, 1–3 daqiqa kuting…")
                want_now = True
        cb = u.get("callback_query")
        if cb:
            if str(cb["from"]["id"]) != ADMIN:
                tg_safe("answerCallbackQuery", {"callback_query_id": cb["id"]})
                continue
            action, _, key = (cb.get("data") or "").partition("|")
            d = state["drafts"].get(key)
            print(f"[tap] {action} {key} -> {d.get('status') if d else 'not found'}")
            if not d or d.get("status") != "pending":
                old = d.get("status") if d else None
                note = {"approved": "Allaqachon tasdiqlangan ✅",
                        "published": "Bu post allaqachon kanalga chiqqan 📢",
                        "skipped": "Bu post o‘tkazib yuborilgan"}.get(
                            old, "Bu post eskirgan. Yangi post uchun /yangi yuboring.")
                tg_safe("answerCallbackQuery", {"callback_query_id": cb["id"], "text": note,
                                                "show_alert": "true"})
                continue
            if action == "ok":
                d["status"] = "approved"
                at = datetime.fromisoformat(d["publish_at"])
                later = at > now_tz()
                when = at.strftime("%H:%M") + " da" if later else "hozir"
                tg_safe("answerCallbackQuery", {"callback_query_id": cb["id"],
                                                "text": f"✅ Tasdiqlandi, kanalga {when} chiqadi"})
                set_button_text(d, f"✅ Tasdiqlandi — kanalga {when} chiqadi.\n{esc(d['title'])}")
            elif action == "no":
                d["status"] = "skipped"
                state["posted"].append(d["link"])          # never offer this article again
                tg_safe("answerCallbackQuery", {"callback_query_id": cb["id"],
                                                "text": "❌ O‘tkazib yuborildi"})
                set_button_text(d, f"❌ O‘tkazib yuborildi. Yangi post tayyorlanmoqda…\n"
                                   f"{esc(d['title'])}")
    return want_now


# ---------------- namaz times ----------------
def fetch_prayer_times(day):
    r = requests.get(f"https://api.aladhan.com/v1/timings/{day.strftime('%d-%m-%Y')}",
                     params={"latitude": LAT, "longitude": LON, "method": PRAYER_METHOD,
                             "timezonestring": "Asia/Tashkent"},
                     headers=UA, timeout=30)
    r.raise_for_status()
    t = r.json()["data"]["timings"]
    return {k: re.match(r"\d{1,2}:\d{2}", t[k]).group() for k, _ in PRAYERS}


def prayer_times(state, day):
    """{'Fajr': '04:53', ...} for this date; fetched once a day and remembered."""
    cache = state.setdefault("prayer_times", {})
    key = day.strftime("%Y-%m-%d")
    if key not in cache:
        try:
            cache[key] = fetch_prayer_times(day)
            print(f"[namaz] {key}: {cache[key]}")
        except Exception as ex:
            print(f"[namaz] could not fetch times: {ex}")
            older = [cache[k] for k in sorted(cache) if k < key]
            return older[-1] if older else DEFAULT_TIMES          # yesterday's times are close
    for k in sorted(cache)[:-3]:
        del cache[k]
    return cache[key]


def today_times(state, now):
    t = prayer_times(state, now)
    return [(uz, t[en]) for en, uz in PRAYERS]


def slots_for(state, now):
    """[(key, slot_datetime, label)] for today (and tomorrow's Bomdod, prepared before midnight)."""
    out = []
    for day in (now, now + timedelta(days=1)):
        times = prayer_times(state, day)
        for en, uz in PRAYERS:
            h, m = map(int, times[en].split(":"))
            at = day.replace(hour=h, minute=m, second=0, microsecond=0)
            if abs((at - now).total_seconds()) < 36 * 3600:
                out.append((at.strftime("%Y-%m-%d_") + en.lower(), at,
                            f"{uz} ({at.strftime('%H:%M')})"))
    return out


# ---------------- main loop ----------------
def handle_slot(state, key, slot, label, now):
    d = state["drafts"].get(key)
    if now < slot - PREPARE_BEFORE:
        return
    if now > slot + LATE_LIMIT:                       # bot was offline too long: skip this slot
        if d and d.get("status") in ("pending", "approved"):
            d["status"] = "expired"
            set_button_text(d, f"⌛ Vaqti o‘tdi, chop etilmadi.\n{esc(d['title'])}")
        return
    if d is None or d.get("status") == "skipped":
        new = make_draft(state, key, slot, label, auto=True)
        if new:
            state["drafts"][key] = new
        else:
            state["drafts"][key] = {"status": "empty", "created": now.isoformat()}
            if new is None:
                say(ADMIN, f"ℹ️ {esc(label)} uchun yangi maqola topilmadi.")
        return
    if now < slot:
        return
    if d.get("status") == "approved":
        publish(state, d)
    elif d.get("status") == "pending":
        created = datetime.fromisoformat(d["created"])
        if now >= created + MIN_REVIEW:               # no answer -> publish automatically
            publish(state, d, auto=True)


def tick(state, want_now):
    """One round of work: namaz-time slots, a manual post if asked, publishing."""
    now = now_tz()
    for key, slot, label in slots_for(state, now):
        try:
            handle_slot(state, key, slot, label, now)
        except Exception:
            traceback.print_exc()

    if want_now:
        key = "m" + now.strftime("%Y%m%d%H%M%S")
        d = make_draft(state, key, now, "Qo‘shimcha post (/yangi)")
        if d:
            state["drafts"][key] = d
        elif d is None:
            say(ADMIN, "ℹ️ Yangi maqola topilmadi.")

    for key, d in list(state["drafts"].items()):    # manual drafts publish right after ✅
        if key.startswith("m") and d.get("status") == "approved":
            publish(state, d)

    cutoff = now - timedelta(days=3)
    state["drafts"] = {k: d for k, d in state["drafts"].items()
                       if datetime.fromisoformat(d.get("created", now.isoformat())) > cutoff}


def persist(state):
    """Save memory to state.json and, on GitHub, push it right away."""
    save_state(state)
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    run = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True)
    run("config", "user.name", "github-actions[bot]")
    run("config", "user.email", "41898283+github-actions[bot]@users.noreply.github.com")
    run("add", STATE_FILE)
    if run("diff", "--cached", "--quiet").returncode == 0:
        return
    run("commit", "-m", "Update bot state")
    for _ in range(3):
        run("pull", "--rebase", "-X", "theirs")
        if run("push").returncode == 0:
            return
        time.sleep(3)
    print("[persist] push failed")


def main():
    state = load_state()
    if not ADMIN or not CHANNEL:
        try:
            process_updates(state)
        except Exception:
            traceback.print_exc()
        print("ADMIN_CHAT_ID or TELEGRAM_CHANNEL is not set yet. Send /start to your bot and "
              "run the workflow; the bot will reply with your chat ID.")
        persist(state)
        return

    deadline = time.time() + LISTEN_MINUTES * 60
    want_now = FORCE_DRAFT
    wait = 0
    while True:
        try:
            want_now = process_updates(state, wait) or want_now
        except Exception:
            traceback.print_exc()
            time.sleep(10)
        try:
            tick(state, want_now)
        except Exception:
            traceback.print_exc()
        want_now = False
        persist(state)
        left = deadline - time.time()
        if left < 5:
            break
        wait = int(min(50, left))       # wait for your next tap / command


if __name__ == "__main__":
    main()
