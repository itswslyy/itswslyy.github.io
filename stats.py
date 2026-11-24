"""Phone stats: fetch every account's numbers, add a snapshot to the history, write an ENCRYPTED stats.enc.
Runs on GitHub every ~15 min (.github/workflows/stats.yml). The phone page (docs/stats/) decrypts it with your
STATS_KEY passphrase, so the public repo only ever holds scrambled data.

Env: IG_TOKEN, FB_TOKEN, THREADS_TOKEN, YT_AUTH (relatable channel), STATS_KEY, optional WEZLA_AUTH.
Each account is skipped (with its error shown on the phone) if its token is missing or fails.
Local test without encryption:  python stats.py --plain out.json
"""
import base64, json, os, sys, time, urllib.error, urllib.parse, urllib.request

FB_G = "https://graph.facebook.com/v23.0/"
TH_G = "https://graph.threads.net/v1.0/"
YT_G = "https://www.googleapis.com/youtube/v3/"
HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json")))  # ig_user_id, page_id (already in the repo)
WEZLA = os.environ.get("WEZLA_CHANNEL") or CFG.get("wezla_channel", "")
OUT = os.environ.get("STATS_OUT", "stats.enc")
KEEP_DAYS = 45


def _open(req):
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            err = json.load(e).get("error", {})
            msg = err.get("message", "") if isinstance(err, dict) else str(err)
        except Exception:
            msg = ""
        raise ValueError(msg or f"HTTP {e.code}")


def _get(base, token, path, **q):
    q["access_token"] = token
    return _open(base + path + "?" + urllib.parse.urlencode(q))


def _n(x):
    return int(x or 0)


# ---------- Instagram ----------
def instagram():
    tok, uid = os.environ.get("IG_TOKEN", ""), CFG.get("ig_user_id")
    if not tok or not uid:
        raise ValueError("IG_TOKEN secret missing")
    prof = _get(FB_G, tok, uid, fields="username,followers_count,media_count")
    base = "id,caption,media_type,media_product_type,like_count,comments_count,timestamp,permalink,thumbnail_url,media_url"
    try:
        items = _get(FB_G, tok, uid + "/media", limit=15, fields=base + ",insights.metric(views,shares,saved)")["data"]
    except ValueError:
        items = _get(FB_G, tok, uid + "/media", limit=15, fields=base)["data"]
    posts = []
    for m in items:
        ins = {d["name"]: d["values"][0]["value"] for d in (m.get("insights") or {}).get("data", []) if d.get("values")}
        posts.append({"title": (m.get("caption") or "").split("\n")[0][:80], "ts": m.get("timestamp"),
                      "url": m.get("permalink"), "kind": "reel" if m.get("media_product_type") == "REELS" else "post",
                      "views": ins.get("views"), "likes": _n(m.get("like_count")), "comments": _n(m.get("comments_count")),
                      "shares": ins.get("shares"), "saves": ins.get("saved")})
    reach = None
    try:  # account reach/views for today (UK day as Meta counts it)
        r = _get(FB_G, tok, uid + "/insights", metric="views,reach", period="day", metric_type="total_value")["data"]
        reach = {d["name"]: d.get("total_value", {}).get("value") for d in r}
    except ValueError:
        pass
    return {"name": "@" + prof.get("username", ""), "url": "https://instagram.com/" + prof.get("username", ""),
            "followers": _n(prof.get("followers_count")), "posts_total": _n(prof.get("media_count")),
            "today": reach, "posts": posts}


# ---------- Facebook Page ----------
def facebook():
    tok, page = os.environ.get("FB_TOKEN") or os.environ.get("IG_TOKEN", ""), CFG.get("page_id")
    if not tok or not page:
        raise ValueError("FB_TOKEN secret missing")
    info = _get(FB_G, tok, page, fields="name,followers_count,fan_count,link")
    reels = _get(FB_G, tok, page + "/video_reels", limit=12, fields="description,created_time,permalink_url,views,"
                 "likes.summary(true).limit(0),comments.summary(true).limit(0)").get("data", [])
    cnt = lambda x, k: (x.get(k) or {}).get("summary", {}).get("total_count", 0)
    posts = [{"title": (r.get("description") or "")[:80], "ts": r.get("created_time"), "kind": "reel",
              "url": ("https://www.facebook.com" + r["permalink_url"]) if (r.get("permalink_url") or "").startswith("/")
              else r.get("permalink_url"), "views": r.get("views"), "likes": cnt(r, "likes"), "comments": cnt(r, "comments")}
             for r in reels]
    return {"name": info.get("name"), "url": info.get("link") or f"https://www.facebook.com/{page}",
            "followers": _n(info.get("followers_count", info.get("fan_count"))), "posts": posts}


# ---------- Threads ----------
def threads():
    tok = os.environ.get("THREADS_TOKEN", "")
    if not tok:
        raise ValueError("THREADS_TOKEN secret missing")
    me = _get(TH_G, tok, "me", fields="id,username")
    out = {"name": "@" + me.get("username", ""), "url": "https://www.threads.net/@" + me.get("username", "")}
    try:
        ins = _get(TH_G, tok, "me/threads_insights", metric="followers_count")["data"]
        out["followers"] = _n(ins[0].get("total_value", {}).get("value"))
    except (ValueError, IndexError, KeyError):
        out["followers"] = None
    posts = []
    for t in _get(TH_G, tok, "me/threads", limit=10, fields="id,text,timestamp,permalink").get("data", []):
        p = {"title": (t.get("text") or "")[:80], "ts": t.get("timestamp"), "url": t.get("permalink"), "kind": "post"}
        try:
            m = {d["name"]: d["values"][0]["value"] for d in
                 _get(TH_G, tok, t["id"] + "/insights", metric="views,likes,replies")["data"] if d.get("values")}
            p.update(views=m.get("views"), likes=m.get("likes"), comments=m.get("replies"))
        except ValueError:
            pass
        posts.append(p)
    out["posts"] = posts
    return out


# ---------- YouTube ----------
_yt_tok = {}


def yt_token(env="YT_AUTH"):
    if env not in _yt_tok:
        a = json.loads(os.environ.get(env) or "{}")
        if not a.get("refresh_token"):
            raise ValueError(env + " secret missing")
        r = _open(urllib.request.Request("https://oauth2.googleapis.com/token", method="POST", data=urllib.parse.urlencode(
            {"client_id": a["client_id"], "client_secret": a["client_secret"], "refresh_token": a["refresh_token"],
             "grant_type": "refresh_token"}).encode()))
        _yt_tok[env] = r["access_token"]
    return _yt_tok[env]


def _yt(p, _env="YT_AUTH", **q):
    return _open(urllib.request.Request(YT_G + p + "?" + urllib.parse.urlencode(q),
                                        headers={"Authorization": "Bearer " + yt_token(_env)}))


def youtube(channel_id=None):
    # wezla is read by its public channel id, with its own login (WEZLA_AUTH) or the relatable one (YT_AUTH)
    env = "WEZLA_AUTH" if channel_id and os.environ.get("WEZLA_AUTH") else "YT_AUTH"
    sel = {"id": channel_id, "_env": env} if channel_id else {"mine": "true"}
    ch = (_yt("channels", part="snippet,statistics,contentDetails", **sel).get("items") or [None])[0]
    if not ch:
        raise ValueError("channel not found")
    st = ch["statistics"]
    up = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    ids = [i["contentDetails"]["videoId"] for i in _yt("playlistItems", env, part="contentDetails", playlistId=up,
                                                        maxResults=15).get("items", [])]
    vids = _yt("videos", env, part="snippet,statistics", id=",".join(ids)).get("items", []) if ids else []
    posts = [{"title": v["snippet"]["title"][:80], "ts": v["snippet"]["publishedAt"], "kind": "video",
              "url": "https://youtu.be/" + v["id"], "views": _n(v["statistics"].get("viewCount")),
              "likes": _n(v["statistics"].get("likeCount")), "comments": _n(v["statistics"].get("commentCount"))}
             for v in vids]
    posts.sort(key=lambda x: x["ts"], reverse=True)
    return {"name": ch["snippet"]["title"], "url": "https://www.youtube.com/channel/" + ch["id"],
            "followers": None if st.get("hiddenSubscriberCount") else _n(st.get("subscriberCount")),
            "views_total": _n(st.get("viewCount")), "posts_total": _n(st.get("videoCount")), "posts": posts}


# ---------- encryption (AES-GCM, key from passphrase via PBKDF2 - same as the phone page) ----------
ITER = 310000


def _key(passphrase, salt):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    return PBKDF2HMAC(hashes.SHA256(), 32, salt, ITER).derive(passphrase.encode())


def encrypt(obj, passphrase):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_key(passphrase, salt)).encrypt(iv, json.dumps(obj, separators=(",", ":")).encode(), None)
    b = lambda x: base64.b64encode(x).decode()
    return {"v": 1, "iter": ITER, "salt": b(salt), "iv": b(iv), "ct": b(ct)}


def decrypt(box, passphrase):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    d = lambda k: base64.b64decode(box[k])
    return json.loads(AESGCM(_key(passphrase, d("salt"))).decrypt(d("iv"), d("ct"), None))


# ---------- main ----------
def main():
    plain = "--plain" in sys.argv
    out = sys.argv[sys.argv.index("--plain") + 1] if plain else OUT
    key = os.environ.get("STATS_KEY", "")
    if not plain and len(key) < 12:
        sys.exit("STATS_KEY secret missing or too short")
    prev = {}
    if os.path.exists(out):
        try:
            prev = json.load(open(out)) if plain else decrypt(json.load(open(out)), key)
        except Exception as e:
            print("Old stats unreadable, starting fresh history:", type(e).__name__)
    jobs = {"instagram": instagram, "youtube": youtube, "facebook": facebook, "threads": threads}
    if WEZLA:
        jobs["wezla"] = lambda: youtube(WEZLA)
    accounts, now = {}, int(time.time())
    for k, fn in jobs.items():
        try:
            accounts[k] = fn()
            print(f"{k}: ok, followers={accounts[k].get('followers')}")
        except Exception as e:
            old = (prev.get("accounts") or {}).get(k)
            accounts[k] = dict(old or {}, error=str(e)[:160], stale=True)
            print(f"{k}: FAILED - {str(e)[:160]}")
    snap = {k: {"f": a.get("followers"), "v": sum(p.get("views") or 0 for p in a.get("posts", []))}
            for k, a in accounts.items() if not a.get("stale")}
    hist = [h for h in prev.get("history", []) if h[0] > now - KEEP_DAYS * 86400] + [[now, snap]]
    data = {"updated": now, "accounts": accounts, "history": hist}
    json.dump(data if plain else encrypt(data, key), open(out, "w"), separators=(",", ":"))
    print("wrote", out, f"({len(hist)} snapshots)")


if __name__ == "__main__":
    main()
