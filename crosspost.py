"""Cross-posting: every post the autopilot puts on Instagram also goes to your Facebook Page, Threads and YouTube Shorts.
Each platform switches on by itself once its GitHub secret is added (FB_TOKEN, THREADS_TOKEN, YT_AUTH).
YouTube only gets Reels. YT_AUTH comes from running youtube_login.py on your PC.
YouTube privacy: state.json -> "crosspost": {"youtube_privacy": "public"} ("private"/"unlisted" also work).
Turn one off without removing the secret: state.json -> "crosspost": {"facebook": false} (or "threads").
Custom captions per platform: state.json -> "crosspost": {"captions": {"threads": {"reel": "...", "feed": "..."}}}.
A failure on one platform never stops the Instagram post or the other platform."""
import datetime, json, os, re, time, urllib.error, urllib.parse, urllib.request, uuid

FB_G = "https://graph.facebook.com/v23.0/"
TH_G = "https://graph.threads.net/v1.0/"
FB_TOKEN = os.environ.get("FB_TOKEN", "")
TH_TOKEN = os.environ.get("THREADS_TOKEN", "")
YT_AUTH = os.environ.get("YT_AUTH", "")
# Hashtags that only mean something on Instagram - dropped elsewhere
IG_ONLY_TAGS = {"explore", "explorepage", "instagram", "instagood", "instadaily", "instareels", "igreels", "reelsinstagram"}
THREADS_MAX = 500


def _open(req, timeout=120):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("error", {}).get("message", "")
        except Exception:
            msg = ""
        raise ValueError(msg or f"HTTP {e.code}")


def _get(base, token, path, **q):
    q["access_token"] = token
    return _open(base + path + "?" + urllib.parse.urlencode(q))


def _post(base, token, path, **params):
    params["access_token"] = token
    return _open(urllib.request.Request(base + path, data=urllib.parse.urlencode(params).encode(), method="POST"))


def _post_file(path, data, filename, **fields):
    """Multipart upload to Facebook (sends the image bytes, so Facebook never has to fetch Instagram's links)."""
    fields["access_token"] = FB_TOKEN
    b = uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    body += (f'--{b}\r\nContent-Disposition: form-data; name="source"; filename="{filename}"\r\n'
             f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
    return _open(urllib.request.Request(FB_G + path, data=body, method="POST",
                                        headers={"Content-Type": "multipart/form-data; boundary=" + b}), timeout=300)


# ---------- captions ----------
def adapt(caption, platform):
    """Instagram caption -> one that fits the platform. Threads allows a single hashtag per post."""
    kept = [0]

    def tag(m):
        if m.group(1).lower() in IG_ONLY_TAGS:
            return ""
        if platform == "threads":
            kept[0] += 1
            if kept[0] > 1:
                return ""
        return m.group(0)

    text = re.sub(r"#(\w+)", tag, caption or "")
    text = "\n".join(re.sub(r"[ \t]{2,}", " ", line).strip() for line in text.splitlines()).strip()
    if platform == "threads" and len(text) > THREADS_MAX:
        text = text[:THREADS_MAX - 1].rstrip() + "…"
    return text


# Facebook doesn't reward Instagram hashtags; it rewards shares and comments, so each post gets a share/comment prompt
# (specific send prompts - Facebook demotes "tag someone"/"comment X" engagement bait)
FB_CTAS = ["Send this to the one who always leaves you on read", "Share this with the friend who'd do this",
           "Send this to your person", "Be honest, is this you?", "Who does this remind you of?",
           "Send this to the one who needs to hear it", "Share this if you've been here",
           "Send this to the group chat"]
FB_FOOTER = "Follow Wslyyofficial for more"


def fb_caption(state):
    st = state.setdefault("crosspost_status", {})
    n = st.get("fb_cta", 0)
    st["fb_cta"] = n + 1
    return FB_CTAS[n % len(FB_CTAS)] + "\n\n" + FB_FOOTER


def caption_for(platform, kind, ig_caption, state, text=None):
    custom = state.get("crosspost", {}).get("captions", {}).get(platform, {}).get(kind)
    if custom is not None:
        return custom
    if platform == "facebook":
        return adapt(text, "facebook") if text else fb_caption(state)  # post's own caption minus Instagram-only tags
    return adapt(ig_caption, platform)


# Facebook gets its own posting times (UK): its audience is on in the day and evening, not at 00:00-06:00 like
# some Instagram slots. Each cross-post is scheduled into the next free slot and Facebook publishes it itself.
FB_SLOTS = (7, 9, 12, 14, 17, 19, 21, 22)


def fb_slot(state):
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/London")
    except Exception:  # Windows without tzdata (local testing only)
        tz = datetime.datetime.now().astimezone().tzinfo
    now = datetime.datetime.now(tz)
    st = state.setdefault("crosspost_status", {})
    used = set(st.get("fb_slots", []))
    for day in range(4):
        d = now.date() + datetime.timedelta(days=day)
        for h in FB_SLOTS:
            t = datetime.datetime.combine(d, datetime.time(h), tz)
            key = t.strftime("%Y-%m-%d %H")
            if t > now + datetime.timedelta(minutes=20) and key not in used:
                st["fb_slots"] = (sorted(used) + [key])[-60:]
                return int(t.timestamp()), key
    return None, None


def enabled(state, cfg):
    s = state.get("crosspost", {})
    on = []
    if FB_TOKEN and cfg.get("page_id") and s.get("facebook", True):
        on.append("facebook")
    if TH_TOKEN and s.get("threads", True):
        on.append("threads")
    if YT_AUTH and s.get("youtube", True):
        on.append("youtube")
    return on


# ---------- Facebook Page ----------
def fb_wait(vid):
    for _ in range(30):  # up to 5 min
        st = _get(FB_G, FB_TOKEN, vid, fields="status").get("status", {})
        if st.get("video_status") == "error" or any(
                (st.get(p) or {}).get("status") == "error" for p in ("uploading_phase", "processing_phase", "publishing_phase")):
            err = (st.get("processing_phase") or {}).get("errors") or st
            raise ValueError(f"Facebook couldn't process the reel: {str(err)[:150]}")
        if st.get("video_status") == "ready" or (st.get("publishing_phase") or {}).get("status") == "complete":
            return
        time.sleep(10)


def facebook(kind, fresh, caption, page, download, when=None):
    """when = unix time to schedule it for (None = publish now). Returns the post's link."""
    sch = {"published": "false", "scheduled_publish_time": str(when)} if when else {}
    if kind == "reel":
        vid = _post(FB_G, FB_TOKEN, page + "/video_reels", upload_phase="start")["video_id"]
        data = download(fresh["media_url"])
        _open(urllib.request.Request(f"https://rupload.facebook.com/video-upload/v23.0/{vid}", data=data, method="POST",
                                     headers={"Authorization": "OAuth " + FB_TOKEN, "offset": "0", "file_size": str(len(data))}),
              timeout=300)
        extra = {"video_state": "SCHEDULED", "scheduled_publish_time": str(when)} if when else {"video_state": "PUBLISHED"}
        _post(FB_G, FB_TOKEN, page + "/video_reels", upload_phase="finish", video_id=vid, description=caption, **extra)
        if not when:
            fb_wait(vid)
        return f"https://www.facebook.com/reel/{vid}"
    if fresh.get("media_type") == "CAROUSEL_ALBUM":
        pics = [c["media_url"] for c in fresh.get("children", {}).get("data", []) if c.get("media_type") == "IMAGE"]
        if not pics:
            raise ValueError("carousel has no photos (videos inside carousels aren't cross-posted to Facebook)")
        if len(pics) > 1:
            tmp = {"temporary": "true"} if when else {}
            ids = [_post_file(page + "/photos", download(u), f"{i}.jpg", published="false", **tmp)["id"]
                   for i, u in enumerate(pics)]
            att = {f"attached_media[{i}]": json.dumps({"media_fbid": x}) for i, x in enumerate(ids)}
            pid = _post(FB_G, FB_TOKEN, page + "/feed", message=caption, **att, **sch)["id"]
            return "https://www.facebook.com/" + pid
        fresh = {"media_url": pics[0]}
    r = _post_file(page + "/photos", download(fresh["media_url"]), "photo.jpg", message=caption, **sch)
    return "https://www.facebook.com/" + r.get("post_id", r["id"])


# ---------- Threads ----------
def th_wait(cid):
    for _ in range(60):  # up to 10 min (videos)
        s = _get(TH_G, TH_TOKEN, cid, fields="status,error_message")
        if s.get("status") == "FINISHED":
            return
        if s.get("status") in ("ERROR", "EXPIRED"):
            raise ValueError("Threads couldn't process it: " + str(s.get("error_message") or s.get("status")))
        time.sleep(10)
    raise ValueError("Threads took too long to process it")


def _th_item(uid, media, **extra):
    if media.get("media_type") == "VIDEO":
        return _post(TH_G, TH_TOKEN, uid + "/threads", media_type="VIDEO", video_url=media["media_url"], **extra)["id"]
    return _post(TH_G, TH_TOKEN, uid + "/threads", media_type="IMAGE", image_url=media["media_url"], **extra)["id"]


def threads(kind, fresh, caption):
    uid = _get(TH_G, TH_TOKEN, "me", fields="id")["id"]
    kids = fresh.get("children", {}).get("data", [])[:20] if fresh.get("media_type") == "CAROUSEL_ALBUM" else []
    if len(kids) >= 2:
        ids = []
        for ch in kids:
            k = _th_item(uid, ch, is_carousel_item="true")
            th_wait(k)
            ids.append(k)
        cid = _post(TH_G, TH_TOKEN, uid + "/threads", media_type="CAROUSEL", children=",".join(ids), text=caption)["id"]
    else:
        media = kids[0] if kids else {"media_type": "VIDEO" if kind == "reel" else "IMAGE", "media_url": fresh["media_url"]}
        cid = _th_item(uid, media, text=caption)
    th_wait(cid)
    tid = _post(TH_G, TH_TOKEN, uid + "/threads_publish", creation_id=cid)["id"]
    try:
        return _get(TH_G, TH_TOKEN, tid, fields="permalink").get("permalink")
    except Exception:
        return None


def refresh_threads_token(state):
    """Threads tokens last 60 days. Refreshing once a week keeps it alive; the expiry is saved in state.json."""
    s = state.setdefault("crosspost_status", {})
    last = s.get("threads_refreshed")
    if not TH_TOKEN or (last and datetime.date.fromisoformat(last) > datetime.date.today() - datetime.timedelta(days=7)):
        return
    try:
        r = _open(TH_G.replace("v1.0/", "") + "refresh_access_token?" + urllib.parse.urlencode(
            {"grant_type": "th_refresh_token", "access_token": TH_TOKEN}))
        s["threads_refreshed"] = datetime.date.today().isoformat()
        s["threads_token_expires"] = (datetime.date.today() + datetime.timedelta(seconds=r.get("expires_in", 0))).isoformat()
        if r.get("access_token") and r["access_token"] != TH_TOKEN:
            s["threads_warning"] = "Threads gave a new token - paste it into the THREADS_TOKEN secret before " + s["threads_token_expires"]
            print("  NOTE: Threads issued a new token; update the THREADS_TOKEN secret (not printed for safety)")
        else:
            s.pop("threads_warning", None)
    except Exception as e:
        s["threads_warning"] = "Threads token refresh failed: " + str(e)[:150]
        print("  Threads token refresh failed:", e)


# ---------- YouTube Shorts ----------
YT_TITLES = ["This is way too relatable 😂", "Tell me this isn't you 😭", "Why is this so accurate 💀", "Every single time 😂",
             "Send this to someone who does this 😭", "Nobody talks about this 😂", "I feel personally attacked 💀",
             "This is literally me 😂", "Be honest, you've done this 😭", "Relatable or just me? 🤔"]


def yt_title(caption, src_id):
    text = re.sub(r"#\w+", "", caption or "").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) < 8:  # caption is only hashtags - pick a title, same post always gets the same one
        text = YT_TITLES[int(src_id or 0) % len(YT_TITLES)]
    return (text[:90].rstrip() + " #shorts")[:100]


def yt_token():
    a = json.loads(YT_AUTH)
    r = _open(urllib.request.Request("https://oauth2.googleapis.com/token", method="POST", data=urllib.parse.urlencode(
        {"client_id": a["client_id"], "client_secret": a["client_secret"], "refresh_token": a["refresh_token"],
         "grant_type": "refresh_token"}).encode()))
    return r["access_token"]


def youtube(kind, fresh, caption, state, download, src_id):
    if kind != "reel":
        return None  # only Reels go to YouTube
    tok = yt_token()
    data = download(fresh["media_url"])
    custom = state.get("crosspost", {}).get("captions", {}).get("youtube", {}).get("reel")
    meta = {"snippet": {"title": custom or yt_title(caption, src_id),
                        "description": (adapt(caption, "youtube") + "\n\n#shorts #relatable #funny").strip(),
                        "tags": ["shorts", "relatable", "funny", "memes", "relatable memes"], "categoryId": "23"},
            "status": {"privacyStatus": state.get("crosspost", {}).get("youtube_privacy", "public"),
                       "selfDeclaredMadeForKids": False}}
    start = urllib.request.Request(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        data=json.dumps(meta).encode(), method="POST",
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(len(data))})
    try:
        with urllib.request.urlopen(start, timeout=60) as r:
            upload_url = r.headers["Location"]
    except urllib.error.HTTPError as e:
        raise ValueError(_yt_err(e))
    try:
        with urllib.request.urlopen(urllib.request.Request(upload_url, data=data, method="PUT",
                                                           headers={"Content-Type": "video/mp4"}), timeout=600) as r:
            vid = json.load(r)["id"]
    except urllib.error.HTTPError as e:
        raise ValueError(_yt_err(e))
    return "https://youtube.com/shorts/" + vid


def _yt_err(e):
    try:
        err = json.load(e).get("error", {})
        reason = (err.get("errors") or [{}])[0].get("reason", "")
        if reason == "quotaExceeded":
            return "YouTube's daily upload limit is used up - it resets at 8am UK time"
        return err.get("message") or f"YouTube error {e.code}"
    except Exception:
        return f"YouTube error {e.code}"


def run(kind, fresh, ig_caption, state, cfg, download, src_id=None, text=None):
    """Post to every enabled platform. Returns {"facebook": {...}, "threads": {...}} for the history log."""
    out = {}
    for platform in enabled(state, cfg):
        if platform == "youtube" and kind != "reel":
            continue
        cap = caption_for(platform, kind, ig_caption, state, text)
        try:
            if platform == "facebook":
                when, key = fb_slot(state) if state.get("crosspost", {}).get("fb_schedule", True) else (None, None)
                try:
                    url = facebook(kind, fresh, cap, cfg["page_id"], download, when)
                    if when:
                        out[platform] = {"status": "scheduled", "for": key, "url": url}
                        print(f"  facebook: scheduled for {key}:00", url)
                        continue
                except Exception as e:
                    if not when:
                        raise
                    print("  facebook: scheduling failed, posting now:", e)
                    url = facebook(kind, fresh, cap, cfg["page_id"], download)
            elif platform == "threads":
                url = threads(kind, fresh, cap)
            else:
                url = youtube(kind, fresh, ig_caption, state, download, src_id)
            out[platform] = {"status": "posted", "url": url}
            print(f"  {platform}: posted", url or "")
        except Exception as e:
            out[platform] = {"status": "failed", "error": str(e)[:200]}
            print(f"  {platform}: FAILED", e)
    return out
