# Deploying PredictSys for free

The whole thing runs on GitHub: no server, no third-party account, and nothing that expires.

## How it fits together

- **The website is static files** (`server/public/`): HTML, JS, CSS, plus JSON snapshots in
  `server/public/data/`. It is published to **GitHub Pages**.
- **The pipeline** (`src/predict.py`: models, ESPN, the SQLite database) runs on **GitHub Actions**
  on a schedule, and every run ends by publishing fresh files to Pages. The laptop never has to be on.
- **The database** (your accumulated track record) is kept between runs as an asset on a GitHub
  release tagged `state` - see `scripts/ci_state.sh`.
- **The repository is public.** That makes Actions minutes unlimited and Pages free. The one catch -
  GitHub switches scheduled workflows off in a public repo after 60 days without activity - is
  handled by `scripts/keepalive.sh`, which every run starts with: if the newest commit is more than
  30 days old, it adds an empty one.

The live address is `https://<your-github-username>.github.io/predictsys/`.

## One-time setup (about 15 minutes)

1. **Protect your email.** GitHub -> Settings -> Emails: tick *Keep my email addresses private* and
   *Block command line pushes that expose my email*. Commits then carry your
   `...@users.noreply.github.com` address instead of your real one.
2. **Create the repository:** name `predictsys`, visibility **Public**, and leave "Add a README",
   ".gitignore" and "license" all unticked - it must be empty.
3. **Push the code:**
   ```bash
   git remote add origin https://github.com/<you>/predictsys.git
   git push -u origin main
   ```
4. **Turn on Pages:** the repository's Settings -> Pages -> Build and deployment -> Source:
   **GitHub Actions**.
5. **Keep your track record** (optional, but without it history restarts from zero):
   ```bash
   bash scripts/ci_state.sh pack      # writes data/state/predictions.db.gz and xg.tar.gz
   ```
   On GitHub: Releases -> Draft a new release -> tag `state` -> attach those two files ->
   **Publish release** (not "Save draft" - the workflow can't see a draft). Until a `state` release
   exists, scheduled runs skip themselves, so nothing can start a fresh history by accident; if you
   skip this step, the manual run below creates the release.
6. **Run it once:** Actions -> *Refresh predictions* -> Run workflow -> `full`. The first run takes
   ~15 minutes (it downloads the history). The finished run shows the site's address; after that it
   runs by itself.

## What runs when

| Schedule | Mode | Does |
|---|---|---|
| every 2 hours | light | national teams, tennis, grading finished matches (~2 min) |
| 05:41 and 17:41 UTC | full | everything, including refitting the club-league models (~10 min) |

Change the times in `.github/workflows/refresh.yml`. GitHub can start a scheduled run 15-60 minutes
late when it is busy; that is normal and harmless at this cadence.

## What keeps it from ever pausing

| What could stop it | Why it doesn't (or what you would see) |
|---|---|
| GitHub's 60-day inactivity rule for public repos | `scripts/keepalive.sh` adds an empty commit when the repo has been quiet for 30 days |
| Running out of free minutes | Standard-runner minutes in a public repo are unlimited |
| A token expiring | There are none - Pages is published with the workflow's own built-in token |
| A new library release breaking the pipeline | `constraints.txt` pins every package to the version that was tested; the actions are pinned to major versions and the runner to `ubuntu-24.04` |
| A data feed being down (ESPN, Understat, football-data) | The run still publishes whatever worked, and is marked failed so you hear about it |
| Anything else | The site shows **"Updated X ago"** and turns red after 6 hours. GitHub also emails whoever last edited the schedule when a run fails (Settings -> Notifications -> Actions) |

## Things worth knowing

- **Local use is unchanged:** `python src/predict.py`, then `npm --prefix server start`.
- **Updating a library on purpose:** upgrade it in the `.venv`, run
  `python -m unittest discover -s tests -t .`, then regenerate the pins with
  `pip freeze > constraints.txt` (keep the comment at the top).
- **Pages limits:** 1 GB site size and a soft 100 GB/month of bandwidth - this site is about 4 MB.
  GitHub's terms don't allow Pages for a commercial service.
- **Licensing:** the tennis history is Jeff Sackmann's CC BY-NC-SA data (non-commercial, credited in
  the page footer), and ESPN's feed is unofficial and not for commercial use. If this ever earns
  money, those two sources need replacing with licensed ones.
- **Club xG data** is scraped from Understat, which may refuse requests from GitHub's servers. That
  is survivable: a failed refresh falls back to the cached copy (kept on the `state` release), and
  the other sports are unaffected.
- **An old Vercel deployment** (`predictsys.vercel.app`) is no longer updated by anything here. Delete
  the project in Vercel's dashboard, or leave it - nothing in this repo touches it.
- **Another host?** Publish the `server/public` folder to any static host and swap the last two
  steps of the workflow.
- **Always-on box instead?** The `Dockerfile` runs the pipeline on a loop and serves the site from
  one container - a paid-tier route, since free hosts don't give containers a persistent disk.
