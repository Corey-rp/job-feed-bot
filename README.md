# Job Feed Bot

Posts new internship and new grad roles to Discord, split into channels by category:
Software Engineering, Front End, Data/AI/ML, Quant, Product, Hardware.

It pulls from these lists, removes duplicates across them, and posts only roles it hasn't seen before:
- SimplifyJobs/Summer2027-Internships
- SimplifyJobs/New-Grad-Positions
- vanshb03/Summer2027-Internships
- vanshb03/New-Grad-2027

It runs on GitHub Actions every 15 minutes for free. No server needed.

## Setup

1. **Create the repo.** Push this folder to a new GitHub repo. Private is fine.
2. **Make webhooks.** In Discord, for each channel go to Edit Channel → Integrations → Webhooks → New Webhook → Copy URL.
3. **Add secrets.** In the repo, go to Settings → Secrets and variables → Actions → New repository secret. Add only the ones you want:

   | Secret | Posts |
   |---|---|
   | `WEBHOOK_SWE` | Software engineering roles |
   | `WEBHOOK_FRONTEND` | Front end / UI / web roles |
   | `WEBHOOK_DATA` | Data science, AI, ML, analytics |
   | `WEBHOOK_QUANT` | Quant and trading |
   | `WEBHOOK_PM` | Product management |
   | `WEBHOOK_HARDWARE` | Hardware, embedded, firmware |
   | `WEBHOOK_DEFAULT` | Anything without its own channel |

   To split internships from new grad, use `WEBHOOK_INTERN` / `WEBHOOK_NEWGRAD`, or a pair like `WEBHOOK_INTERN_SWE` / `WEBHOOK_NEWGRAD_SWE`. The most specific secret wins.

4. **Allow commits.** Go to Settings → Actions → General → Workflow permissions and select **Read and write permissions**. The bot commits `seen.json` so it remembers what it already posted.
5. **Run it once.** Go to Actions → Job Feed → Run workflow. The first run only records the roughly 16k existing roles and posts nothing, so your server doesn't get flooded. After that, new roles post every 15 minutes.

## Customizing (`config.json`)
- `sources`: add or remove any repo that publishes a `listings.json`.
- `categories`: delete one to stop posting it.
- `max_age_days`: skips roles posted longer ago than this, which matters when you add a new source.

## Test locally
```
DRY_RUN=1 python bot.py
```
This prints what would post and sends nothing. Requires only Python 3.9+, with no packages.
