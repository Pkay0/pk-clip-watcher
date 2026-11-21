#!/usr/bin/env python3
"""Poll Nitter RSS feeds for new video clips.

Stdout is exactly two lines so GitHub Actions can parse them:
    NEW <n>
    CLIPS <json list of clips found this run>

State and health written next to this script are durable fields only.
Run timestamps stay in watch.log so a quiet run does not dirty git.
"""
import json
import logging
import os
import random
import shutil
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HERE = os.environ.get("WATCH_DIR", SCRIPT_DIR)
STATE = os.path.join(HERE, "state.json")
LOG = os.path.join(HERE, "watch.log")
HEALTH = os.path.join(HERE, "health.json")
TIMEOUT = float(os.environ.get("WATCH_TIMEOUT", "20"))
HANDLES = ["scubaryan_", "FearedBuck", "yoxics"]
# PRIMARY were reliable and fresh; SECONDARY OK but often served cached feeds 20+ min stale; kareem.one mostly 429.
PRIMARY = ["nitter.mie.foundation", "nitter.jaydenha.uk"]
SECONDARY = ["nitter.netbub.com", "ruf.st", "nitter.kareem.one"]
INSTANCES = PRIMARY + SECONDARY
FEEDS_WANTED = 2  # merge this many good feeds per account so one stale instance can't hide a post
if os.environ.get("WATCH_INSTANCES"):
    INSTANCES = [i.strip() for i in os.environ["WATCH_INSTANCES"].split(",") if i.strip()]
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
DC = {"dc": "http://purl.org/dc/elements/1.1/"}

log = logging.getLogger("watch")
log.setLevel(logging.INFO)
log.propagate = False
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
try:
    os.makedirs(HERE, exist_ok=True)
    _fh = logging.FileHandler(LOG, encoding="utf-8")
    _fh.setLevel(logging.INFO)
    _fh.setFormatter(_fmt)
    log.addHandler(_fh)
except Exception as _e:
    sys.stderr.write(f"cannot open log file {LOG}: {_e}\n")
_sh = logging.StreamHandler(sys.stderr)
_sh.setLevel(logging.WARNING)
_sh.setFormatter(_fmt)
log.addHandler(_sh)

pending = []
emitted = False
DEADLINE = time.time() + float(os.environ.get("WATCH_BUDGET", "300"))


def load(path, default):
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, type(default)):
            raise ValueError(f"expected {type(default).__name__}, got {type(data).__name__}")
        return data
    except FileNotFoundError:
        return default
    except Exception as e:
        log.error("could not read %s (%s); backing it up and using empty default", path, e)
        try:
            os.replace(path, path + ".corrupt-" + time.strftime("%Y%m%d%H%M%S"))
        except Exception:
            pass
        return default


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def serialize(data):
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def save(path, data):
    """Atomically write JSON. No-op when the bytes are unchanged."""
    text = serialize(data)
    old = None
    try:
        with open(path) as f:
            old = f.read()
    except FileNotFoundError:
        old = None
    except Exception:
        old = None
    if old == text:
        return False
    if path == STATE and old is not None:
        try:
            json.loads(old)
            shutil.copyfile(path, path + ".bak")
        except Exception:
            pass
    parent = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise
    return True


def canonical_state(state):
    if not isinstance(state, dict):
        return {}
    out = {}
    for h in HANDLES:
        entry = state.get(h)
        if not isinstance(entry, dict) or entry.get("last_id") in (None, ""):
            continue
        out[h] = {"last_id": str(entry["last_id"])}
    return out


def canonical_health(health):
    """Durable health. Timestamps are logged, not stored, so quiet runs do not commit."""
    if not isinstance(health, dict):
        health = {}
    out = {}
    for h in HANDLES:
        entry = health.get(h)
        fails = 0
        if isinstance(entry, dict):
            try:
                fails = int(entry.get("consecutive_failures", 0) or 0)
            except (TypeError, ValueError):
                fails = 0
        out[h] = {"consecutive_failures": fails}
    try:
        streak = int(health.get("all_failed_streak", 0) or 0)
    except (TypeError, ValueError):
        streak = 0
    out["all_failed_streak"] = streak
    out["alert"] = bool(health.get("alert", False))
    out["alert_sent"] = bool(health.get("alert_sent", False))
    return out


def last_id_of(state, handle):
    try:
        return int(state.get(handle, {}).get("last_id", 0) or 0)
    except (TypeError, ValueError):
        return 0


def emit(added):
    global emitted
    if emitted:
        return
    emitted = True
    print(f"NEW {len(added)}")
    print("CLIPS " + json.dumps(added, ensure_ascii=False))
    sys.stdout.flush()


def fetch(handle, min_id=0):
    if os.environ.get("WATCH_INSTANCES"):
        insts = INSTANCES[:]
    else:
        p, sec = PRIMARY[:], SECONDARY[:]
        random.shuffle(p)
        random.shuffle(sec)
        insts = p + sec
    merged, used = {}, []
    fallback = None
    for inst in insts:
        if time.time() > DEADLINE:
            log.error("time budget exceeded, skipping remaining instances for %s", handle)
            break
        base = inst if "://" in inst else f"https://{inst}"
        url = f"{base}/{handle}/rss"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                ctype = r.headers.get("Content-Type", "")
                body = r.read()
            if "xml" not in ctype:
                log.warning("%s %s: non-RSS response (%s), likely bot wall", inst, handle, ctype)
                continue
            root = ET.fromstring(body)
            posts = []
            for it in root.findall(".//item"):
                creator = (it.findtext("dc:creator", namespaces=DC) or "").lstrip("@")
                title = it.findtext("title") or ""
                desc = it.findtext("description") or ""
                pid = (it.findtext("guid") or "").strip()
                if not pid.isdigit():
                    continue
                if creator.lower() != handle.lower():
                    continue
                if title.startswith("R to @") or title.startswith("RT by @"):
                    continue
                u = urllib.parse.unquote(desc)
                is_video = ("ext_tw_video_thumb/" in u or "amplify_video_thumb/" in u) or \
                           ("<br>Video<br>" in desc and "tweet_video_thumb" not in u)
                posts.append({"handle": handle, "id": pid,
                              "url": f"https://x.com/{handle}/status/{pid}",
                              "text": title, "created_at": it.findtext("pubDate") or "",
                              "video": is_video})
            if not root.findall(".//item"):
                log.warning("%s %s: RSS had 0 items", inst, handle)
                continue
            newest = max((int(p["id"]) for p in posts), default=0)
            if min_id and newest < min_id:
                log.warning("%s %s: stale feed (newest %s < known %s), trying next", inst, handle, newest, min_id)
                fallback = fallback or (inst, posts)
                continue
            for p_ in posts:
                if p_["id"] not in merged or p_["video"]:
                    merged[p_["id"]] = p_
            used.append(inst)
            if len(used) >= FEEDS_WANTED:
                break
        except Exception as e:
            log.warning("%s %s: %s", inst, handle, e)
            continue
    if used:
        return "+".join(used), list(merged.values())
    if fallback:
        inst, posts = fallback
        log.info("using stale fallback %s for %s", inst, handle)
        return inst, posts
    log.error("all instances failed for %s", handle)
    return None, None


def mark_alert_sent():
    health = canonical_health(load(HEALTH, {}))
    if not health["alert"]:
        log.warning("not marking alert_sent; alert is false")
        return
    if health["alert_sent"]:
        log.info("alert_sent already true")
        return
    health["alert_sent"] = True
    save(HEALTH, health)
    log.info("alert_sent set true at %s", now())


def main():
    global pending
    state = load(STATE, {})
    if not state and os.path.exists(STATE + ".bak"):
        state = load(STATE + ".bak", {})
    state = canonical_state(state)
    health = canonical_health(load(HEALTH, {}))
    added = []
    for h in HANDLES:
        last = last_id_of(state, h)
        inst, posts = fetch(h, last)
        if posts is None:
            health[h]["consecutive_failures"] = int(health[h]["consecutive_failures"]) + 1
            log.info("%s failure consecutive=%s at %s", h, health[h]["consecutive_failures"], now())
            continue
        health[h]["consecutive_failures"] = 0
        newest = max([int(p["id"]) for p in posts], default=last)
        log.info("%s ok via %s newest=%s last=%s at %s", h, inst, newest, last, now())
        if last == 0:
            state[h] = {"last_id": str(newest)}
            log.info("seeded %s at %s (%s)", h, newest, now())
            continue
        for p in sorted(posts, key=lambda x: int(x["id"])):
            if int(p["id"]) <= last or not p["video"]:
                continue
            rec = {k: p[k] for k in ("handle", "id", "url", "text", "created_at")}
            rec["found_at"] = now()
            added.append(rec)
            log.info("new clip %s %s", rec["handle"], rec["id"])
        if newest > last:
            state[h] = {"last_id": str(newest)}
            log.info("advanced %s last_id %s -> %s (%s)", h, last, newest, now())
    fails = [h for h in HANDLES if health[h]["consecutive_failures"] > 0]
    if len(fails) == len(HANDLES):
        health["all_failed_streak"] = int(health["all_failed_streak"]) + 1
    else:
        health["all_failed_streak"] = 0
    health["alert"] = health["all_failed_streak"] >= 2
    if not health["alert"]:
        health["alert_sent"] = False
    log.info(
        "last_run=%s new=%s alert=%s alert_sent=%s streak=%s failures=%s",
        now(), len(added), health["alert"], health["alert_sent"],
        health["all_failed_streak"],
        {h: health[h]["consecutive_failures"] for h in HANDLES},
    )
    save(HEALTH, health)
    save(STATE, state)
    pending = added
    emit(added)


if __name__ == "__main__":
    mark = any(a == "--mark-alert-sent" for a in sys.argv[1:])
    try:
        if mark:
            mark_alert_sent()
        else:
            main()
    except Exception as e:
        try:
            log.exception("fatal: %s", e)
        except Exception:
            sys.stderr.write(f"fatal: {e}\n")
        if not mark:
            try:
                emit(pending)
            except Exception:
                pass
    sys.exit(0)
