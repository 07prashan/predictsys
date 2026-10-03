# Deploying PredictSys for free

## How it fits together

- **The website is static files** (`server/public/`): HTML, JS, CSS, plus JSON snapshots in
  `server/public/data/`. Any static host serves it - it's on **Vercel** now.
- **The pipeline** (`src/predict.py`: models, ESPN, the SQLite database) can't run on Vercel - no
  persistent disk, and the Python/ML stack is far too big for a function. It runs on **GitHub
  Actions** (free) instead, on a schedule, and each run ends by deploying fresh files to Vercel.
- **The database** (your accumulated track record) is kept between runs as an asset on a GitHub
  release tagged `state` - see `scripts/ci_state.sh`.

Live site: https://predictsys.vercel.app - deployed once by hand, so for now it only changes when
something redeploys it. The steps below make that automatic.

## One-time setup (about 10 minutes)

1. **Put the code on GitHub.** Create an empty repository, then:
   ```bash
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin master
   ```
   A public repo has unlimited free Actions minutes. A private one has 2,000 a month; the default
   schedule (below) uses roughly 1,600 of them.
2. **Keep your track record** (optional, but without it history restarts from zero):
   ```bash
   bash scripts/ci_state.sh pack      # writes data/state/predictions.db.gz and xg.tar.gz
   ```
   On GitHub: Releases -> Draft a new release -> tag `state` -> attach those two files -> Publish.
3. **Add three repository secrets** (Settings -> Secrets and variables -> Actions):
   - `VERCEL_TOKEN` - create one at https://vercel.com/account/tokens
   - `VERCEL_ORG_ID` and `VERCEL_PROJECT_ID` - the two values in `server/public/.vercel/project.json`

   Until all three exist the workflow still refreshes the predictions; it just skips the deploy and
   says so.
4. **Run it once:** Actions -> *Refresh predictions* -> Run workflow -> `full`. The first run takes
   ~15 minutes (it downloads the history); after that it runs by itself.

## What runs when

| Schedule | Mode | Does |
|---|---|---|
| every 2 hours | light | national teams, tennis, grading finished matches (~2 min) |
| 05:41 and 17:41 UTC | full | everything, including refitting the club-league models (~10 min) |

Change the times in `.github/workflows/refresh.yml`. The site shows **"Updated X ago"** and turns it
red after 6 hours, so a stalled schedule is visible to anyone looking.

## Things worth knowing

- **Deploying by hand:** `cd server/public && npx vercel@latest deploy --prod --yes` (use `@latest` -
  an old global Vercel CLI is rejected by Vercel's API). Run `python src/predict.py` first to refresh
  the data.
- **Local use is unchanged:** `python src/predict.py`, then `npm --prefix server start`.
- **Free-tier limits:** Vercel Hobby allows 100 deployments a day (this uses ~14) and is for
  non-commercial use. In a *public* repo GitHub pauses scheduled workflows after 60 days with no
  repository activity - push any commit (or re-enable it in the Actions tab) to wake it.
- **Licensing:** the tennis history is Jeff Sackmann's CC BY-NC-SA data (non-commercial, credited in
  the page footer), and ESPN's feed is unofficial and not for commercial use. If this ever earns
  money, those two sources need replacing with licensed ones.
- **Club xG data** is scraped from Understat, which may refuse requests from GitHub's servers. That
  is survivable: a failed refresh falls back to the cached copy (kept on the `state` release), and
  the other sports are unaffected.
- **Another host?** Deploy the `server/public` folder to any static host (Netlify, Cloudflare
  Pages, GitHub Pages) and swap the last deploy step in the workflow.
- **Always-on box instead?** The `Dockerfile` runs the pipeline on a loop and serves the site from
  one container - a paid-tier route, since free hosts don't give containers a persistent disk.
