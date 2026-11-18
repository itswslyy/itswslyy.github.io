"""Cross-posting: every post the autopilot puts on Instagram also goes to your Facebook Page and Threads.
Each platform switches on by itself once its GitHub secret is added (FB_TOKEN, THREADS_TOKEN).
Turn one off without removing the secret: state.json -> "crosspost": {"facebook": false} (or "threads").
Custom captions per platform: state.json -> "crosspost": {"captions": {"threads": {"reel": "...", "feed": "..."}}}.
A failure on one platform never stops the Instagram post or the other platform."""
import datetime, json, os, re, time, urllib.error, urllib.parse, urllib.request, uuid

FB_G = "https://graph.facebook.com/v23.0/"
TH_G = "https://graph.threads.net/v1.0/"
FB_TOKEN = os.environ.get("FB_TOKEN", "")
TH_TOKEN = os.environ.get("THREADS_TOKEN", "")
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


def caption_for(platform, kind, ig_caption, state):
    custom = state.get("crosspost", {}).get("captions", {}).get(platform, {}).get(kind)
    return custom if custom is not None else adapt(ig_caption, platform)


def enabled(state, cfg):
    s = state.get("crosspost", {})
    on = []
    if FB_TOKEN and cfg.get("page_id") and s.get("facebook", True):
        on.append("facebook")
    if TH_TOKEN and s.get("threads", True):
        on.append("threads")
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


def facebook(kind, fresh, caption, page, download):
    if kind == "reel":
        vid = _post(FB_G, FB_TOKEN, page + "/video_reels", upload_phase="start")["video_id"]
        data = download(fresh["media_url"])
        _open(urllib.request.Request(f"https://rupload.facebook.com/video-upload/v23.0/{vid}", data=data, method="POST",
                                     headers={"Authorization": "OAuth " + FB_TOKEN, "offset": "0", "file_size": str(len(data))}),
              timeout=300)
        _post(FB_G, FB_TOKEN, page + "/video_reels", upload_phase="finish", video_id=vid, video_state="PUBLISHED",
              description=caption)
        fb_wait(vid)
        return f"https://www.facebook.com/reel/{vid}"
    if fresh.get("media_type") == "CAROUSEL_ALBUM":
        pics = [c["media_url"] for c in fresh.get("children", {}).get("data", []) if c.get("media_type") == "IMAGE"]
        if not pics:
            raise ValueError("carousel has no photos (videos inside carousels aren't cross-posted to Facebook)")
        if len(pics) > 1:
            ids = [_post_file(page + "/photos", download(u), f"{i}.jpg", published="false")["id"] for i, u in enumerate(pics)]
            att = {f"attached_media[{i}]": json.dumps({"media_fbid": x}) for i, x in enumerate(ids)}
            pid = _post(FB_G, FB_TOKEN, page + "/feed", message=caption, **att)["id"]
            return "https://www.facebook.com/" + pid
        fresh = {"media_url": pics[0]}
    r = _post_file(page + "/photos", download(fresh["media_url"]), "photo.jpg", message=caption)
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


def run(kind, fresh, ig_caption, state, cfg, download):
    """Post to every enabled platform. Returns {"facebook": {...}, "threads": {...}} for the history log."""
    out = {}
    for platform in enabled(state, cfg):
        cap = caption_for(platform, kind, ig_caption, state)
        try:
            url = facebook(kind, fresh, cap, cfg["page_id"], download) if platform == "facebook" else threads(kind, fresh, cap)
            out[platform] = {"status": "posted", "url": url}
            print(f"  {platform}: posted", url or "")
        except Exception as e:
            out[platform] = {"status": "failed", "error": str(e)[:200]}
            print(f"  {platform}: FAILED", e)
    return out
