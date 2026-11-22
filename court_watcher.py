#!/usr/bin/env python3
"""
London basketball court watcher.

Watches several London venues for 2 back-to-back hours of basketball court
time between 16:00 and 21:00. When it finds a block it:
  1. sends a push notification to your phone (ntfy app) with one-tap links, and
  2. for Better venues: logs in and adds both hours to your Better basket.
     (Battersea uses a different system, Places Leisure, so for that one you
     get the alert + link and book it yourself.)

Runs on your Mac or in the cloud (GitHub Actions, see README.md).

Usage:
  python3 court_watcher.py --check         # just print matching blocks
  python3 court_watcher.py --once          # check once; on a match: alert + basket
  python3 court_watcher.py --loop          # keep watching (Mac must stay awake)
  python3 court_watcher.py --cloud         # one GitHub Actions run (see README)
  python3 court_watcher.py --test-alert    # send a test notification to your phone
  python3 court_watcher.py --login         # Mac only: log in to Better once in a browser window

Environment variables (set as GitHub secrets in the cloud):
  NTFY_TOPIC       your private ntfy topic name (phone alerts)
  BETTER_EMAIL     Better email or customer ID   } only needed for auto-basket
  BETTER_PASSWORD  Better password               } in the cloud
  HEADLESS=1       run the browser invisibly (set automatically by --cloud)
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")

# ----------------------------- settings ------------------------------------
# Venues in priority order. Only one block is handled per date, and the first
# venue in this list with a free block wins. Delete or comment out any you
# don't want.
VENUES = [
    {"name": "Score (Leyton)", "system": "better", "slug": "score-leisure-centre",
     "activities": ["basketball-full-60min", "basketball-half-60min"]},
    {"name": "Battersea Sports Centre", "system": "places", "site": "229",
     "activities": {"229A001305": "Full court",
                    "229A001306": "Half court (1-2)",
                    "229A001307": "Half court (3-4)"}},
    {"name": "Crystal Palace NSC", "system": "better", "slug": "crystal-palace-leisure-centre",
     "activities": ["basketball-full-60min"]},
    {"name": "Phoenix (White City)", "system": "better",
     "slug": "phoenix-fitness-centre-and-janet-adegoke-swimming-pool",
     "activities": ["basketball-full-60min"]},
    {"name": "Finsbury (Old Street)", "system": "better", "slug": "finsbury-leisure-centre",
     "activities": ["basketball-full-60min"]},
    # More Better venues with evening basketball (pricier, so off by default):
    # {"name": "Talacre (Kentish Town)", "system": "better",
    #  "slug": "talacre-community-sports-centre", "activities": ["basketball-full-60min"]},
    # {"name": "King's Cross", "system": "better", "slug": "kings-cross-fitness",
    #  "activities": ["basketball-half-60min"]},
]
WINDOW_START = "16:00"      # earliest start
WINDOW_END = "21:00"        # latest finish
HOURS = 2                   # consecutive 60-minute slots
WEEKDAYS = {0, 1, 2, 3, 4, 5, 6}   # 0=Mon … 6=Sun; e.g. {1, 3} for Tue & Thu
SAME_COURT_ONLY = True      # False = accept two different courts if needed
AUTO_BASKET = True          # False = phone alert only
POLL_MINUTES = 10           # normal polling interval in --loop
# ---------------------------------------------------------------------------

BETTER_DAYS_AHEAD = 7       # Better shows 6 days ahead; one extra is harmless
BETTER_RELEASE = "22:00"    # Better releases a new day at 10pm
PLACES_DAYS_AHEAD = 9       # Places releases each slot 8 days ahead, at that slot's time

HERE = Path(__file__).resolve().parent
PROFILE_DIR = HERE / "better-profile"
STATE_FILE = HERE / "watcher_state.json"
BETTER_API = "https://better-admin.org.uk/api/activities"
BETTER_SITE = "https://bookings.better.org.uk"
BETTER_BASKET = BETTER_SITE + "/basket"
PLACES_SITE = "https://placesleisure.gladstonego.cloud"
HEADERS = {
    "Accept": "application/json",
    "Origin": BETTER_SITE,
    "Referer": BETTER_SITE + "/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
}


def now_london():
    return dt.datetime.now(LONDON)


def log(msg):
    print(f"[{now_london():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def hm_to_min(s):
    h, m = map(int, s.split(":"))
    return h * 60 + m


# ----------------------------- alerts --------------------------------------
def mac_notify(title, message):
    if sys.platform != "darwin":
        return
    script = f'display notification {json.dumps(message)} with title {json.dumps(title)} sound name "Glass"'
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=10)
    except Exception:
        pass


def phone_alert(title, message, click_url=None, actions=None, priority="high"):
    """Push notification via ntfy.sh to the topic in NTFY_TOPIC."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    headers = {"Title": title.encode("ascii", "ignore").decode(),
               "Priority": priority, "Tags": "basketball"}
    if click_url:
        headers["Click"] = click_url
    if actions:   # [(label, url), ...]  -> buttons on the notification
        headers["Actions"] = "; ".join(f"view, {label}, {url}" for label, url in actions)
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as e:
        log(f"  phone alert failed: {e}")
        return False


def alert(title, message, click_url=None, actions=None):
    log(f"ALERT: {title} - {message}")
    mac_notify(title, message)
    phone_alert(title, message, click_url, actions)


# ----------------------------- state ---------------------------------------
def load_state():
    try:
        state = json.loads(STATE_FILE.read_text())
    except Exception:
        state = {}
    state.setdefault("basketed", [])
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


# ----------------------------- Better venues -------------------------------
def better_slots(venue_slug, activity, date):
    url = f"{BETTER_API}/venue/{venue_slug}/activity/{activity}/v2/slots?date={date}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r).get("data")
    except urllib.error.HTTPError as e:
        if e.code == 422:   # date outside the bookable window
            return []
        raise
    return data if isinstance(data, list) else []


def bhm(s):
    return s["starts_at"]["format_24_hour"], s["ends_at"]["format_24_hour"]


def chain_blocks(by_start, end_of, loc_of):
    """Given {start: [slots]} find HOURS-long chains, preferring one court.
    Returns a list of lists of slots."""
    blocks = []
    for start in sorted(by_start):
        chain = [start]
        for _ in range(HOURS - 1):
            nxt = end_of(by_start[chain[-1]][0])
            if nxt not in by_start:
                break
            chain.append(nxt)
        if len(chain) != HOURS:
            continue
        loc_sets = [{loc_of(s): s for s in by_start[t]} for t in chain]
        common = set(loc_sets[0]).intersection(*loc_sets[1:])
        if common:
            loc = sorted(common)[0]
            blocks.append([ls[loc] for ls in loc_sets])
        elif not SAME_COURT_ONLY:
            blocks.append([by_start[t][0] for t in chain])
    return blocks


def better_slot_url(venue_slug, slot):
    start, end = bhm(slot)
    return (f"{BETTER_SITE}/location/{venue_slug}/{slot['category_slug']}/{slot['date']['raw']}"
            f"/by-time/slot/{start}-{end}/{slot['composite_key']}")


def scan_better(venue, dates):
    """Yield normalised blocks for one Better venue."""
    # Fetch every (date, activity) in parallel - matters during the 10pm race
    jobs = [(d, a) for d in dates for a in venue["activities"]]
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {job: ex.submit(better_slots, venue["slug"], job[1], job[0]) for job in jobs}
    for date in dates:
        for act in venue["activities"]:
            try:
                slots = futures[(date, act)].result()
            except Exception as e:
                log(f"  error reading {venue['name']} {act} {date}: {e}")
                continue
            ok = [s for s in slots
                  if (s.get("action_to_show") or {}).get("status") == "BOOK" and (s.get("spaces") or 0) > 0
                  and bhm(s)[0] >= WINDOW_START and bhm(s)[1] <= WINDOW_END]
            by_start = {}
            for s in ok:
                by_start.setdefault(bhm(s)[0], []).append(s)
            blocks = chain_blocks(by_start, lambda s: bhm(s)[1], lambda s: s["location"]["id"])
            if blocks:
                b = blocks[0]
                yield {
                    "venue": venue["name"], "system": "better", "slug": venue["slug"],
                    "date": date, "start": bhm(b[0])[0], "end": bhm(b[-1])[1],
                    "kind": "Full court" if "full" in act else "Half court",
                    "court": b[0]["location"]["name"].split(",")[0],
                    "price": sum((s.get("price") or {}).get("raw") or 0 for s in b) / 100,
                    "book_url": better_slot_url(venue["slug"], b[0]),
                    "slots": b, "note": "",
                }
                break   # first activity type (e.g. full court) wins for this date


# ----------------------------- Places venues (Battersea) -------------------
def places_sessions(activity_id, _cache={}):
    """Load Places' public booking calendar like a visitor and read the
    availability data the page receives. Needs Playwright."""
    if activity_id in _cache and time.time() - _cache[activity_id][0] < 15:
        return _cache[activity_id][1]
    from playwright.sync_api import sync_playwright
    got = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        def on_response(r):
            if "/api/availability/V2/sessions" in r.url and r.status == 200:
                try:
                    got.append(r.json())
                except Exception:
                    pass
        page.on("response", on_response)
        page.goto(f"{PLACES_SITE}/book/calendar/{activity_id}", wait_until="networkidle", timeout=60000)
        for _ in range(20):
            if got:
                break
            page.wait_for_timeout(500)
        browser.close()
    data = got[0] if got else []
    _cache[activity_id] = (time.time(), data)
    return data


def parse_utc(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def scan_places(venue, dates):
    now = now_london()
    wanted = set(dates)
    for act_id, kind in venue["activities"].items():
        try:
            days = places_sessions(act_id)
        except Exception as e:
            log(f"  error reading {venue['name']} {kind}: {e}")
            continue
        for day in days:
            if day.get("date") not in wanted:
                continue
            for loc in day.get("locations", []):
                by_start = {}
                for s in loc.get("slots", []):
                    st = parse_utc(s["startTime"]).astimezone(LONDON)
                    # slots are 55 min; treat as ending on the hour
                    en = (parse_utc(s["endTime"]) + dt.timedelta(seconds=1)).astimezone(LONDON)
                    en = en + dt.timedelta(minutes=(-en.minute) % 60)
                    s["_start"], s["_end"] = f"{st:%H:%M}", f"{en:%H:%M}"
                    if s.get("status") == "Available" and s["_start"] >= WINDOW_START and s["_end"] <= WINDOW_END:
                        by_start.setdefault(s["_start"], []).append(s)
                blocks = chain_blocks(by_start, lambda s: s["_end"], lambda s: loc["locationNameToDisplay"])
                for b in blocks:
                    # Only report once the first hour can actually be booked
                    if parse_utc(b[0]["bookableFrom"]) > now:
                        continue
                    later = [s for s in b[1:] if parse_utc(s["bookableFrom"]) > now]
                    note = ""
                    if later:
                        opens = parse_utc(later[0]["bookableFrom"]).astimezone(LONDON)
                        note = f"The {later[0]['_start']} hour opens at {opens:%H:%M} today - book it then."
                    yield {
                        "venue": venue["name"], "system": "places",
                        "date": day["date"], "start": b[0]["_start"], "end": b[-1]["_end"],
                        "kind": kind, "court": loc["locationNameToDisplay"], "price": None,
                        "book_url": f"{PLACES_SITE}/book/calendar/{act_id}",
                        "slots": b, "note": note,
                    }
                    break


# ----------------------------- scanning ------------------------------------
def scan(systems=None):
    """Return the best block per date across all venues (venue order = priority).
    systems: optional set like {"better"} to only check those booking systems."""
    today = now_london().date()

    def dates(n):
        return [(today + dt.timedelta(days=i)).isoformat() for i in range(n + 1)
                if (today + dt.timedelta(days=i)).weekday() in WEEKDAYS]

    best = {}
    for venue in VENUES:
        if systems and venue["system"] not in systems:
            continue
        scanner =scan_better if venue["system"] == "better" else scan_places
        n = BETTER_DAYS_AHEAD if venue["system"] == "better" else PLACES_DAYS_AHEAD
        try:
            for blk in scanner(venue, dates(n)):
                best.setdefault(blk["date"], blk)
        except Exception as e:
            log(f"  error scanning {venue['name']}: {e}")
    return [best[d] for d in sorted(best)]


def describe(b):
    day = dt.date.fromisoformat(b["date"]).strftime("%a %d %b")
    price = f", £{b['price']:.2f}" if b.get("price") else ""
    return f"{day} {b['start']}-{b['end']}, {b['venue']}, {b['kind']} ({b['court']}){price}"


# ----------------------------- browser (Better basket) ---------------------
def headless():
    return os.environ.get("HEADLESS", "") not in ("", "0", "false")


def open_browser(p):
    return p.chromium.launch_persistent_context(
        str(PROFILE_DIR), headless=headless(), viewport={"width": 1200, "height": 900})


def dismiss_cookies(page):
    btn = page.get_by_role("button", name="Reject All")
    if btn.count():
        try:
            btn.first.click(timeout=2000)
        except Exception:
            pass


def logged_out(page):
    return page.get_by_role("button", name="Log in", exact=True).count() > 0


def ensure_logged_in(page, venue_slug):
    page.goto(f"{BETTER_SITE}/location/{venue_slug}", wait_until="networkidle")
    dismiss_cookies(page)
    if not logged_out(page):
        return True
    email, pw = os.environ.get("BETTER_EMAIL"), os.environ.get("BETTER_PASSWORD")
    if not (email and pw):
        return False
    log("  logging in to Better")
    page.get_by_role("button", name="Log in", exact=True).first.click()
    page.locator("#username").fill(email)
    page.locator("#password").fill(pw)
    page.locator("#password").press("Enter")
    try:
        page.wait_for_function(
            "() => ![...document.querySelectorAll('button')].some(b => b.textContent.trim() === 'Log in')",
            timeout=20000)
    except Exception:
        pass
    page.wait_for_timeout(1500)
    return not logged_out(page)


def add_slot_to_basket(page, venue_slug, slot):
    start, end = bhm(slot)
    page.goto(better_slot_url(venue_slug, slot), wait_until="networkidle")
    dismiss_cookies(page)
    add_btn = page.get_by_role("button", name="Add to basket")
    add_btn.wait_for(timeout=15000)

    # Pick the specific court in the react-select "Select location" dropdown
    court_name = slot["location"]["name"]
    combo = page.get_by_role("combobox", name="Select location")
    if combo.count():
        combo.first.focus()
        page.keyboard.press("ArrowDown")      # opens the react-select menu
        option = page.get_by_role("option", name=court_name)
        if option.count() == 0:
            option = page.get_by_text(court_name, exact=False)
        option.first.click(timeout=5000)

    add_btn.click()
    page.wait_for_timeout(3000)
    log(f"  added {start}-{end} on {court_name.split(',')[0]}")


def basket_better_block(blk):
    """Log in and add every slot in the block to the Better basket."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "Playwright isn't installed"
    with sync_playwright() as p:
        ctx = open_browser(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            if not ensure_logged_in(page, blk["slug"]):
                return False, "couldn't log in to Better"
            for slot in blk["slots"]:
                add_slot_to_basket(page, blk["slug"], slot)
            page.goto(BETTER_BASKET, wait_until="networkidle")
            page.screenshot(path=str(HERE / "basket.png"), full_page=True)
            if not headless():
                log("Both slots are in your basket. Pay in the browser window, then close it.")
                try:
                    page.wait_for_event("close", timeout=0)
                except Exception:
                    pass
            return True, ""
        except Exception as e:
            try:
                page.screenshot(path=str(HERE / "error.png"), full_page=True)
            except Exception:
                pass
            return False, f"basket step failed: {e}"
        finally:
            ctx.close()


def do_login():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(BETTER_SITE)
        print("Log in to Better in the browser window, then close the window.")
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        ctx.close()
    print("Session saved in", PROFILE_DIR)


# ----------------------------- main ----------------------------------------
def handle_match(blk):
    desc = describe(blk)
    log("MATCH: " + desc)
    extra = f"\n{blk['note']}" if blk["note"] else ""

    if blk["system"] != "better" or not AUTO_BASKET:
        alert("Basketball court free - book now!", f"{desc}{extra}\nTap to book.",
              blk["book_url"], [("Book", blk["book_url"])])
        return

    links = [("Open basket", BETTER_BASKET), ("Book slot", blk["book_url"])]
    alert("Basketball court found", desc + "\nAdding to your basket...", blk["book_url"], links)
    ok, reason = basket_better_block(blk)
    if ok:
        alert("Court in your basket - pay now!",
              desc + "\nOpen your Better basket and pay before it expires.", BETTER_BASKET, links)
    else:
        alert("Book it yourself - auto-basket failed",
              f"{desc}\n({reason}) Tap to book.", blk["book_url"], links)


def run_once(dry_run=False, systems=None):
    state = load_state()
    found = scan(systems)
    if not found:
        log(f"No {HOURS}h block between {WINDOW_START} and {WINDOW_END} at any venue.")
        return
    for blk in found:
        if dry_run:
            log("MATCH: " + describe(blk) + (f"  [{blk['note']}]" if blk["note"] else ""))
            continue
        if blk["date"] in state["basketed"]:
            log(f"Already handled {blk['date']}; skipping ({describe(blk)})")
            continue
        handle_match(blk)
        state["basketed"].append(blk["date"])
        save_state(state)


# ----------------------------- timing --------------------------------------
def release_times():
    """Times of day (London) when new slots appear."""
    times = []
    if any(v["system"] == "better" for v in VENUES):
        times.append(BETTER_RELEASE)
    if any(v["system"] == "places" for v in VENUES):
        # Places opens each slot at its own start time, 8 days ahead
        s, e = hm_to_min(WINDOW_START), hm_to_min(WINDOW_END) - 60 * HOURS
        times += [f"{m // 60:02d}:00" for m in range(s, e + 1, 60)]
    return sorted(set(times))


def current_burst(t, before=1, after=8):
    """If t is inside a release burst, return that release datetime."""
    for hm in release_times():
        h, m = map(int, hm.split(":"))
        rel = t.replace(hour=h, minute=m, second=0, microsecond=0)
        if rel - dt.timedelta(minutes=before) <= t <= rel + dt.timedelta(minutes=after):
            return rel
    return None


def seconds_until_next_poll():
    t = now_london()
    if current_burst(t):
        return 20
    nxt = t + dt.timedelta(minutes=POLL_MINUTES)
    for hm in release_times():
        h, m = map(int, hm.split(":"))
        pre = t.replace(hour=h, minute=m, second=0, microsecond=0) - dt.timedelta(minutes=1)
        if t < pre < nxt:
            nxt = pre
    return max(1, (nxt - t).total_seconds())


def burst_systems(release):
    """Which booking systems to poll during a release burst. At Better's 10pm
    release only Better is checked: Battersea releases nothing then, and its
    pages are slow to load, which would slow down the race for Better slots."""
    if release is not None and f"{release:%H:%M}" == BETTER_RELEASE:
        return {"better"}
    return None


def run_cloud():
    """One GitHub Actions run. If a release is coming up within 25 minutes
    (or just happened), stay and poll through it; otherwise check once."""
    os.environ.setdefault("HEADLESS", "1")
    t = now_london()
    upcoming = current_burst(t, before=25, after=8)
    if not upcoming:
        run_once()
        return
    end = upcoming + dt.timedelta(minutes=8)
    systems = burst_systems(upcoming)
    log(f"Release at {upcoming:%H:%M}: polling until {end:%H:%M}"
        + (f" ({', '.join(sorted(systems))} only)" if systems else ""))
    run_once()   # one full check first
    while now_london() < end:
        time.sleep(20 if current_burst(now_london()) else 60)
        try:
            run_once(systems=systems)
        except Exception as e:
            log(f"error: {e}")


def main():
    global WINDOW_START, WINDOW_END, AUTO_BASKET
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--login", action="store_true", help="Mac: open a browser to log in to Better once")
    g.add_argument("--check", action="store_true", help="print matches only")
    g.add_argument("--once", action="store_true", help="check once and act on a match")
    g.add_argument("--loop", action="store_true", help="keep watching")
    g.add_argument("--cloud", action="store_true", help="one GitHub Actions run")
    g.add_argument("--test-alert", action="store_true", help="send a test phone notification")
    ap.add_argument("--window", help="override time window, e.g. 13:00-16:00")
    ap.add_argument("--no-basket", action="store_true", help="alert only, don't add to basket")
    args = ap.parse_args()
    if args.window:
        WINDOW_START, WINDOW_END = args.window.split("-")
    if args.no_basket:
        AUTO_BASKET = False

    if args.login:
        return do_login()
    if args.test_alert:
        if not os.environ.get("NTFY_TOPIC"):
            sys.exit("Set NTFY_TOPIC first.")
        ok = phone_alert("Court watcher test", "Phone alerts are working.", BETTER_SITE, priority="default")
        print("sent" if ok else "failed")
        return 0 if ok else 1
    if args.check:
        run_once(dry_run=True)
        return 0
    if args.once:
        run_once()
        return 0
    if args.cloud:
        run_cloud()
        return 0
    log(f"Watching {len(VENUES)} venues for {HOURS}h of basketball between "
        f"{WINDOW_START}-{WINDOW_END}. Ctrl+C to stop.")
    while True:
        try:
            run_once(systems=burst_systems(current_burst(now_london())))
        except Exception as e:
            log(f"error: {e}")
        time.sleep(seconds_until_next_poll())


if __name__ == "__main__":
    sys.exit(main())
