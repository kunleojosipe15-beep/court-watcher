# London basketball court watcher

Watches London venues for **2 back-to-back hours of basketball court time between 16:00 and 21:00**.

| Venue (priority order) | Booking system | Price / hour | When it finds a block |
|---|---|---|---|
| Score (Leyton) | Better | £50.20 | phone alert + **adds to your basket** |
| Walthamstow Leisure Centre | Better | £17.35 per 40 min (single court) | phone alert + **adds to your basket** |
| Feel Good Centre (Walthamstow) | Better | £25.10 (half court) | phone alert + **adds to your basket** |
| Barking Sporthouse | Better | £25 per 40 min (single court) | phone alert + **adds to your basket** |
| Battersea Sports Centre | Places Leisure | see site | phone alert + link (you book) |
| Crystal Palace National Sports Centre | Better | £45.00 | phone alert + **adds to your basket** |
| Phoenix (White City) | Better | £52.20 | phone alert + **adds to your basket** |
| Finsbury (Old Street) | Better | £90.90 | phone alert + **adds to your basket** |

Prices are full court. The watcher tries full court first, then half court where a venue offers it.
It handles only one block per date, and the first venue in the list wins.
Talacre (£150.80) and King's Cross (£145) also have evening basketball. They're in the code but switched off.

Battersea uses a different booking system (Places Leisure), so for Battersea you get the alert and a link, and you book
yourself with a free Places account.

## When it checks

- **Better venues** release each day 6 days ahead at **22:00**. The watcher polls every 20 seconds from 21:59 to 22:08.
- **Battersea** releases each slot **8 days ahead at that slot's own time**. For example, the 18:00 slot on 30 Nov opened at 18:00 on 22 Nov.
  The watcher polls through 16:00, 17:00, 18:00 and 19:00 each day. For a 4–6pm block, the 5pm hour opens an hour after the 4pm one.
  The alert says when, so you can book the second hour then.
- **Every 2 hours** overnight and in the morning, for cancellations.

## Setup (about 10 minutes)

### 1. Phone alerts (ntfy)
1. Install **ntfy** on your phone (free, App Store / Play Store).
2. Tap **+** and subscribe to your private topic name (the one you saved as the `NTFY_TOPIC` secret).
   Anyone who knows this name can read the alerts, so keep it to yourself.

### 2. Put the code on GitHub
1. Sign in at github.com and make a **new public repository**, e.g. `court-watcher`.
   *Why public: GitHub Actions is free and unlimited for public repos. This watcher uses around 4,000 minutes a month, and private repos get 2,000 free.
   Your secrets stay encrypted and hidden either way. Only the code and the list of dates it handled are visible.*
2. On the empty repo page click **uploading an existing file**. Drag in `court_watcher.py`, `requirements.txt`,
   `README.md`, `.gitignore` and the `.github` folder, then **Commit changes**.

### 3. Add your secrets (encrypted, only the workflow can read them)
Repo → **Settings → Secrets and variables → Actions → New repository secret**, three times:

| Name | Value |
|---|---|
| `NTFY_TOPIC` | your private topic name, e.g. `score-courts-` plus random letters |
| `BETTER_EMAIL` | your Better email or customer ID |
| `BETTER_PASSWORD` | your Better password |

### 4. Test it
Repo → **Actions → Court watcher → Run workflow**, then open the run to see the log.

## Settings (top of `court_watcher.py`)

- `VENUES`: reorder to change priority, or delete or comment out venues.
- `WINDOW_START` / `WINDOW_END`, `HOURS`: your time rule.
- `WEEKDAYS`: currently `{5, 6}` (Saturdays and Sundays). E.g. `{1, 3}` for Tuesdays and Thursdays.
- `MAX_PER_WEEK`: currently 2 bookings per week (Mon–Sun). `None` for no limit.
- `SAME_COURT_ONLY`: set to `False` to accept two different courts.
- `AUTO_BASKET`: set to `False` for phone alerts only.

## Good to know

- **You still pay.** Pay soon after the alert, because baskets expire.
- Each date is handled once (tracked in `watcher_state.json`). Delete a date from that file to watch that day again.
- GitHub pauses scheduled workflows after **60 days with no repo activity** and emails you first.
  Click "Enable workflow" on the Actions tab to keep it going.
- If auto-basket fails, the run keeps a screenshot under **Artifacts** for 7 days.
- On your Mac: `.venv/bin/python court_watcher.py --check` lists what's free right now.
