"""Cloud Autopilot (runs on GitHub Actions every hour, no PC needed).
Reels 00/06/12/18, feed posts 03/09/15/21 (UK time). December posts are saved for December.
Oldest -> newest, loops forever; after each full loop the best-performing reposts come round first.
Set DRY_RUN=1 to see what would post without posting."""
import datetime, hashlib, json, os, sys, time, urllib.error, urllib.parse, urllib.request
import crosspost
from zoneinfo import ZoneInfo

try:
    TZ = ZoneInfo("Europe/London")
except Exception:  # Windows without tzdata (local testing only) - GitHub's Linux servers have it
    TZ = datetime.datetime.now().astimezone().tzinfo
G = "https://graph.facebook.com/v23.0/"
SLOTS = {"reel": (0, 6, 12, 18), "feed": (3, 9, 15, 21)}
EARLY, LATE = 45, 180  # GitHub runs roughly hourly at random minutes: an early run waits for the slot, a missed slot is posted up to 3h late
DRY = os.environ.get("DRY_RUN") == "1"
TOKEN = os.environ.get("IG_TOKEN", "")
cfg = json.load(open("config.json", encoding="utf-8"))
IG = cfg["ig_user_id"]


def _open(req):
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise ValueError(json.load(e).get("error", {}).get("message", f"HTTP {e.code}"))


def get(path, **q):
    q["access_token"] = TOKEN
    return _open(G + path + "?" + urllib.parse.urlencode(q))


def post(path, **params):
    params["access_token"] = TOKEN
    return _open(urllib.request.Request(G + path, data=urllib.parse.urlencode(params).encode(), method="POST"))


def wait(cid):
    for _ in range(90):  # up to 15 min
        s = get(cid, fields="status_code,status")
        if s.get("status_code") == "FINISHED":
            return
        if s.get("status_code") == "ERROR":
            raise ValueError("Instagram couldn't process it: " + s.get("status", ""))
        time.sleep(10)
    raise ValueError("Instagram took too long to process it")


_dl = {}


def download(url):
    """Downloads are kept for the run so cross-posting doesn't fetch the same file twice."""
    if url not in _dl:
        with urllib.request.urlopen(url, timeout=300) as r:
            _dl[url] = r.read()
    return _dl[url]


def upload_video(url, **params):
    """Instagram often can't fetch its own video links, so download the video and upload the file directly."""
    data = download(url)
    cid = post(IG + "/media", upload_type="resumable", **params)["id"]
    _open(urllib.request.Request(f"https://rupload.facebook.com/ig-api-upload/v23.0/{cid}", data=data, method="POST",
                                 headers={"Authorization": "OAuth " + TOKEN, "offset": "0", "file_size": str(len(data))}))
    return cid


def smart_order(main, state):
    views = {}
    for new_id, src in state.get("map", {}).items():
        try:
            v = get(new_id + "/insights", metric="views")["data"][0]["values"][0]["value"]
            views.setdefault(src, []).append(v)
        except Exception:
            pass
    score = {k: sum(v) / len(v) for k, v in views.items()}
    return [x["id"] for x in sorted(main, key=lambda x: (-score.get(x["id"], -1), x["ts"]))]


def pick(kind, now, state, lib):
    lib = [x for x in lib if x["kind"] == kind]
    dec = [x for x in lib if x["month"] == 12]
    main = [x for x in lib if x["month"] != 12] or dec
    p = state.setdefault("ptr", {}).setdefault(kind, {"main": 0, "dec": 0, "dec_year": None})
    if now.month == 12:
        if p.get("dec_year") != now.year:
            p.update(dec=0, dec_year=now.year)
        if p["dec"] < len(dec):
            p["dec"] += 1
            return dec[p["dec"] - 1]
    if p.get("order"):
        pos = {i: n for n, i in enumerate(p["order"])}
        main = sorted(main, key=lambda x: pos.get(x["id"], 10 ** 9))
    item = main[p["main"] % len(main)]
    p["main"] = (p["main"] + 1) % len(main)
    if p["main"] == 0:
        p["loop"] = p.get("loop", 0) + 1
        p["order"] = smart_order(main, state) if not DRY else None
    return item


class NoMedia(Exception):
    pass


def publish(item, caption):
    fresh = get(item["id"], fields="media_type,media_url,children{media_type,media_url}")
    if "media_url" not in fresh and fresh.get("media_type") != "CAROUSEL_ALBUM":
        raise NoMedia("Instagram won't give out this post's file (usually copyrighted music), skipped")
    if item["kind"] == "reel":
        cid = upload_video(fresh["media_url"], media_type="REELS", caption=caption, share_to_feed="true")
    elif fresh.get("media_type") == "CAROUSEL_ALBUM":
        kids = []
        for ch in fresh.get("children", {}).get("data", []):
            if ch.get("media_type") == "VIDEO":
                k = upload_video(ch["media_url"], is_carousel_item="true", media_type="VIDEO")
            else:
                k = post(IG + "/media", is_carousel_item="true", image_url=ch["media_url"])["id"]
            wait(k)
            kids.append(k)
        cid = post(IG + "/media", media_type="CAROUSEL", children=",".join(kids), caption=caption)["id"]
    else:
        cid = post(IG + "/media", image_url=fresh["media_url"], caption=caption)["id"]
    wait(cid)
    new_id = post(IG + "/media_publish", creation_id=cid)["id"]
    return new_id, get(new_id, fields="permalink").get("permalink"), fresh


def next_new(state, kind="feed"):
    """Next freshly made tweet post from queue.json (made by tweetmaker.py) that hasn't been posted yet and is live
    on GitHub Pages (Pages takes a minute or two to publish after a push)."""
    try:
        queue = json.load(open("queue.json", encoding="utf-8"))
    except Exception:
        return None
    done = set(state.get("new_done", []))
    for q in queue:
        if q["id"] in done or q.get("kind", "feed") != kind:
            continue
        try:
            with urllib.request.urlopen(urllib.request.Request(q["url"], method="HEAD"), timeout=20):
                return q
        except Exception:
            print(f"  new post {q['id']} isn't live on GitHub Pages yet, using a repost this time")
            return None
    return None


def publish_new(q, caption):
    reel = q.get("kind") == "reel"
    if q.get("urls"):  # carousel of tweet slides
        kids = []
        for u in q["urls"]:
            k = post(IG + "/media", is_carousel_item="true", image_url=u)["id"]
            wait(k)
            kids.append(k)
        cid = post(IG + "/media", media_type="CAROUSEL", children=",".join(kids), caption=caption)["id"]
        wait(cid)
        new_id = post(IG + "/media_publish", creation_id=cid)["id"]
        return new_id, get(new_id, fields="permalink").get("permalink"), {
            "media_type": "CAROUSEL_ALBUM", "children": {"data": [{"media_type": "IMAGE", "media_url": u} for u in q["urls"]]}}
    if reel:
        cid = upload_video(q["url"], media_type="REELS", caption=caption, share_to_feed="true")
    else:
        cid = post(IG + "/media", image_url=q["url"], caption=caption)["id"]
    wait(cid)
    new_id = post(IG + "/media_publish", creation_id=cid)["id"]
    return new_id, get(new_id, fields="permalink").get("permalink"), {"media_type": "VIDEO" if reel else "IMAGE",
                                                                         "media_url": q["url"]}


def main():
    state = json.load(open("state.json", encoding="utf-8"))
    if not state.get("on", True):
        print("Autopilot is switched off in state.json")
        return
    lib = json.load(open("library.json", encoding="utf-8"))
    targets = crosspost.enabled(state, cfg)
    print("Cross-posting to:", ", ".join(targets) or "nothing (add FB_TOKEN / THREADS_TOKEN / YT_AUTH secrets to switch on)")
    now = datetime.datetime.now(TZ)
    done = set(state.setdefault("slots_done", []))
    n_before = len(done)
    before_status = json.loads(json.dumps(state.get("crosspost_status")))
    for kind, hours in SLOTS.items():
        for day in (now.date() - datetime.timedelta(days=1), now.date(), now.date() + datetime.timedelta(days=1)):
            for h in hours:
                slot = datetime.datetime.combine(day, datetime.time(h), TZ)
                key = f"{slot:%Y-%m-%d %H}:00 {kind}"
                if key in done or not (slot - datetime.timedelta(minutes=EARLY) <= now <= slot + datetime.timedelta(minutes=LATE)):
                    continue
                if now < slot and not DRY:
                    print(f"Waiting {int((slot - now).total_seconds() // 60)} min for the {slot:%H:%M} slot")
                    time.sleep((slot - now).total_seconds())
                state["slots_done"] = (state["slots_done"] + [key])[-200:]
                done.add(key)
                caption = state.get("captions", {}).get(kind, "")
                q = next_new(state, kind)
                if q:  # freshly made tweet posts / Reels go before reposts
                    print(f"{key}: new {kind} {q['id']}: {q['text'][:60]!r}")
                    if DRY:
                        continue
                    rec = {"slot": key, "kind": kind, "new": q["id"], "cloud": True}
                    cap = q.get("caption") or caption  # each new post has its own caption + hashtags
                    try:
                        new_id, url, fresh = publish_new(q, cap)
                        state.setdefault("new_done", []).append(q["id"])
                        rec.update(status="posted", url=url)
                        if state.get("first_comment"):
                            try:
                                post(new_id + "/comments", message=state["first_comment"])
                            except Exception as e:
                                rec["error"] = "first comment failed: " + str(e)[:120]
                        print("  posted", url)
                        if targets:
                            rec["also"] = crosspost.run(kind, fresh, cap, state, cfg, download, q["id"], text=q.get("caption"))
                        _dl.clear()
                        state.setdefault("history", []).insert(0, rec)
                        state["history"] = state["history"][:300]
                        continue
                    except Exception as e:  # fall back to a repost so the slot isn't empty
                        _dl.clear()
                        rec.update(status="failed", error=str(e)[:200])
                        print("  FAILED", e)
                        state.setdefault("history", []).insert(0, rec)
                        fails = state.setdefault("new_fails", {})
                        fails[q["id"]] = fails.get(q["id"], 0) + 1
                        if fails[q["id"]] >= 2:  # give up on it so it doesn't block the queue
                            state.setdefault("new_done", []).append(q["id"])
                bad =state.setdefault("unusable", [])
                for attempt in range(3):  # if a post can't be reposted, use the next one so the slot isn't empty
                    item = pick(kind, now, state, lib)
                    while item["id"] in bad:
                        item = pick(kind, now, state, lib)
                    print(f"{key}: {kind} originally from {item['ts'][:10]} ({item['id']})")
                    if DRY:
                        for t in targets:
                            if t == "youtube":
                                if kind == "reel":
                                    print(f"  would also post to youtube: {crosspost.yt_title(caption, item['id'])!r}")
                                continue
                            print(f"  would also post to {t}: {crosspost.caption_for(t, kind, caption, state)!r}")
                        break
                    rec = {"slot": key, "kind": kind, "orig": item["ts"][:10], "src": item["id"], "cloud": True}
                    try:
                        new_id, url, fresh = publish(item, caption)
                    except Exception as e:
                        if isinstance(e, NoMedia):
                            bad.append(item["id"])  # Instagram won't hand out this file (usually copyrighted music) - skip it for good
                        _dl.clear()
                        rec.update(status="failed", error=str(e)[:200])
                        print("  FAILED", e)
                        state.setdefault("history", []).insert(0, rec)
                        rec = None
                        continue
                    rec.update(status="posted", url=url)
                    state.setdefault("posted_ids", []).append(new_id)
                    state.setdefault("map", {})[new_id] = item["id"]
                    if state.get("first_comment"):
                        try:
                            post(new_id + "/comments", message=state["first_comment"])
                        except Exception as e:
                            rec["error"] = "first comment failed: " + str(e)[:120]
                    print("  posted", url)
                    if targets:
                        rec["also"] = crosspost.run(kind, fresh, caption, state, cfg, download, item["id"])
                    _dl.clear()
                    break
                if DRY or rec is None:
                    continue
                state.setdefault("history", []).insert(0, rec)
                state["history"] = state["history"][:300]
    if not DRY:
        crosspost.refresh_threads_token(state)
    if not DRY and (len(done) > n_before or state.get("crosspost_status") != before_status):
        state["last_run"] = now.isoformat(timespec="minutes")
        json.dump(state, open("state.json", "w", encoding="utf-8"), indent=1)  # only saved when something posted


if __name__ == "__main__":
    if not TOKEN and not DRY:
        sys.exit("IG_TOKEN secret is missing")
    main()
