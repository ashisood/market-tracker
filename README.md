# market-tracker

A daily market-news agent for a shared list of stocks. Every weekday morning a
GitHub Action asks Claude to research the holdings with web search and saves a
short Markdown brief to [`briefs/`](briefs/), one file per day
(`briefs/2026-11-02.md`, …).

Each brief has:

- **Alerts** for any holding that trips a threshold in `config.yaml`
- **Market overview** in two or three sentences
- **Holdings** table (last price, change, headline) plus notes per stock
- **What to watch** for the next few days
- **Sources** linking to the web pages Claude cited

Briefs are automated summaries, not investment advice. Check the figures
against the linked sources before acting on them.

## Add a stock

Holdings live in [`stocks.yaml`](stocks.yaml). To add one, open a pull
request that appends an entry:

```yaml
  - symbol: "TSLA"          # ticker, in quotes
    name: Tesla             # company name, helps the news search
    notes: Delivery numbers # optional: what the brief should keep an eye on
```

The **Validate** check runs on every pull request and fails if a symbol is
malformed, duplicated, or missing a name. Quote tickers: YAML reads a bare
`ON` or `NO` as true/false.

## Tune the alerts

[`config.yaml`](config.yaml) controls the brief:

| Setting | Meaning |
|---|---|
| `alerts.price_move_pct` | Flag a move of at least this % since the previous close |
| `alerts.volume_spike_ratio` | Flag volume at least this many times the recent average |
| `alerts.week52_proximity_pct` | Flag a price within this % of its 52-week high or low |
| `alerts.news_topics` | Flag news on these topics from the last 24 hours |
| `model` / `effort` | Claude model and effort level (`low` … `max`) |
| `max_searches` | Most web searches per brief; caps cost and run time |
| `timezone` | Decides the brief's date |

## Setup

1. **Add your API key.** In the repo on GitHub, go to *Settings → Secrets and
   variables → Actions* and add a secret named `ANTHROPIC_API_KEY`. The
   account needs web search enabled.
2. **Allow the workflow to commit.** In *Settings → Actions → General →
   Workflow permissions*, choose *Read and write permissions*. If `main` is
   protected, let `github-actions[bot]` push to it, or the commit step will
   fail.
3. **Try it.** Under *Actions → Daily market brief*, click *Run workflow*.
   After that it runs automatically at 12:30 UTC Monday to Friday (8:30 am New
   York in summer, 7:30 am in winter). It also runs on market holidays.

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python market_brief.py --validate   # check stocks.yaml and config.yaml
python market_brief.py --dry-run    # show the prompt, no API call
export ANTHROPIC_API_KEY=...        # then:
python market_brief.py              # writes briefs/<today>.md
python -m unittest discover -s tests
```

`--date YYYY-MM-DD` sets the brief's date, and `--out-dir` writes elsewhere.

## How it works

`market_brief.py` loads and validates both YAML files, then makes one
streaming request to the Claude API (`claude-opus-5-5`, adaptive thinking,
the `web_search` server tool). Claude decides what to search for, reads the
results, and writes the brief. If the search loop pauses partway, the script
resumes it. If a safety filter declines the request, the API retries it on
Anthropic's recommended fallback model. The script then keeps only the brief
text, adds the cited sources and a footer, and writes the file. The workflow
commits it to `main`.

| File | Purpose |
|---|---|
| `stocks.yaml` | Holdings (changed by pull request) |
| `config.yaml` | Alert thresholds and model settings |
| `market_brief.py` | Generates the brief |
| `.github/workflows/daily-brief.yml` | Weekday schedule; commits the brief |
| `.github/workflows/validate.yml` | Checks YAML and runs tests on pull requests |
| `briefs/` | One Markdown brief per day |
