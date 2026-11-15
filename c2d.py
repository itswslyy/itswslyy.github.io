"""Cloud Comment-to-DM (runs on GitHub Actions with the Autopilot, no PC needed).
Every new comment on ANY post gets one private-reply DM about the YouTube channel - never the same person twice.
Instagram allows a private reply up to 7 days after the comment. Usernames are stored only as hashes (this repo is public).
Set DRY_RUN=1 to see who would get a DM without sending."""
import concurrent.futures, datetime, hashlib, json, os, re, sys, urllib.error, urllib.parse, urllib.request

G = "https://graph.facebook.com/v23.0/"
DRY = os.environ.get("DRY_RUN") == "1"
TOKEN = os.environ.get("IG_TOKEN", "")
IG = json.load(open("config.json", encoding="utf-8"))["ig_user_id"]
STATE = "c2d_state.json"


def _open(req):
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("error", {}).get("message", "")
        except Exception:
            msg = ""
        raise ValueError(msg or f"HTTP {e.code}")


def get(path, **q):
    if path.startswith("http"):
        return _open(path)
    q["access_token"] = TOKEN
    return _open(G + path + "?" + urllib.parse.urlencode(q))


def post_json(path, body):
    return _open(urllib.request.Request(G + path + "?" + urllib.parse.urlencode({"access_token": TOKEN}),
                                        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"))


def h(user):
    return hashlib.sha256((user or "").lower().encode()).hexdigest()[:16]


def ptime(s):
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")


def first_name(user):
    m = re.match(r"[A-Za-z]+", user or "")
    return m.group(0).capitalize() if m and len(m.group(0)) > 1 else "there"

def main():
    s = json.load(open(STATE, encoding="utf-8"))
    if not s.get("on"):
        print("Comment-to-DM is switched off in", STATE)
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    since = max(datetime.datetime.fromisoformat(s["since"]), now - datetime.timedelta(days=7))
    # every post's comment count (a few calls), then only fetch comments where the count went up
    page = get(IG + "/media", fields="id,comments_count", limit=100)
    media = {m["id"]: m.get("comments_count", 0) for m in page.get("data", [])}
    while page.get("paging", {}).get("next"):
        page = get(page["paging"]["next"])
        media.update({m["id"]: m.get("comments_count", 0) for m in page.get("data", [])})
    seen = s.setdefault("counts", {})
    ids = [mid for mid, n in media.items() if n > seen.get(mid, 0)]

    def fetch(mid):
        try:
            return mid, get(mid, fields="comments.limit(50){id,username,timestamp}")
        except Exception as e:
            print("  couldn't read comments of", mid, e)
            return mid, None
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results = list(pool.map(fetch, ids))
    print(f"{len(media)} posts, {len(ids)} with new comments")

    me = s.get("me", "").lower()
    users, done = set(s.setdefault("users", [])), set(s.setdefault("done", []))
    sent = 0
    for mid, res in results:
        if res is None:
            continue  # count not updated, retried next run
        for cm in res.get("comments", {}).get("data", []):
            u = cm.get("username", "").lower()
            if cm["id"] in done or u == me or ptime(cm["timestamp"]) < since:
                continue
            if h(u) in users:
                done.add(cm["id"]); s["done"].append(cm["id"]); continue
            text = s["message"].replace("{name}", first_name(cm["username"])).replace("{link}", s.get("link", ""))
            if DRY:
                print("  would DM a commenter on", mid); sent += 1; continue
            done.add(cm["id"]); s["done"].append(cm["id"])
            try:
                post_json(s["page_id"] + "/messages", {"recipient": {"comment_id": cm["id"]}, "message": {"text": text}})
                users.add(h(u)); s["users"].append(h(u)); sent += 1
                s.setdefault("history", []).insert(0, {"time": now.isoformat(timespec="minutes"), "ok": True})
            except Exception as e:
                print("  DM failed:", e)
                s.setdefault("history", []).insert(0, {"time": now.isoformat(timespec="minutes"), "ok": False, "error": str(e)[:150]})
        seen[mid] = media[mid]
    for mid, n in media.items():  # deleted comments: track the lower count
        if mid in seen and n < seen[mid]:
            seen[mid] = n
    s["counts"] = {mid: n for mid, n in seen.items() if mid in media}
    s["sent"] = s.get("sent", 0) + (0 if DRY else sent)
    s["done"] = s["done"][-5000:]
    s["history"] = s.get("history", [])[:100]
    s["last_run"] = now.isoformat(timespec="minutes")
    print(("would send " if DRY else "sent ") + str(sent) + " DMs")
    if not DRY:
        json.dump(s, open(STATE, "w", encoding="utf-8"), indent=1)


if __name__ == "__main__":
    if not TOKEN and not DRY:
        sys.exit("IG_TOKEN secret is missing")
    main()
