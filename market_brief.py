#!/usr/bin/env python3
"""Generate the daily market brief for the holdings in stocks.yaml.

Usage:
    python market_brief.py              # research and write briefs/<date>.md
    python market_brief.py --validate   # only check stocks.yaml and config.yaml
    python market_brief.py --dry-run    # print the prompt without calling the API
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

ROOT = Path(__file__).resolve().parent
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")
EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}
BRIEF_HEADING = "# Market Brief"
MAX_CONTINUATIONS = 5  # resumes allowed when the server-side search loop pauses
MAX_TOKENS = 64000


class ConfigError(Exception):
    """stocks.yaml or config.yaml is missing or malformed."""


class BriefError(Exception):
    """The API call did not produce a usable brief."""


# --------------------------------------------------------------------------
# Loading and validating the YAML files
# --------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{path.name} not found") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must contain a mapping at the top level")
    return data


def load_holdings(path: Path) -> list[dict]:
    items = _load_yaml(path).get("holdings")
    if not isinstance(items, list) or not items:
        raise ConfigError(f"{path.name}: 'holdings' must be a non-empty list")

    holdings, seen = [], set()
    for i, item in enumerate(items, 1):
        where = f"{path.name}: holding #{i}"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} must be a mapping with 'symbol' and 'name'")
        raw_symbol = item.get("symbol")
        if not isinstance(raw_symbol, str):
            raise ConfigError(
                f"{where}: symbol {raw_symbol!r} must be a quoted string, e.g. symbol: \"ON\""
            )
        symbol = raw_symbol.strip().upper()
        if not SYMBOL_RE.match(symbol):
            raise ConfigError(f"{where}: {raw_symbol!r} is not a valid ticker symbol")
        if symbol in seen:
            raise ConfigError(f"{where}: {symbol} is listed more than once")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{where} ({symbol}) needs a 'name'")
        notes = item.get("notes") or ""
        if not isinstance(notes, str):
            raise ConfigError(f"{where} ({symbol}): 'notes' must be text")
        seen.add(symbol)
        holdings.append({"symbol": symbol, "name": name.strip(), "notes": notes.strip()})
    return holdings


def _positive_number(alerts: dict, key: str, path: Path) -> float:
    value = alerts.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{path.name}: alerts.{key} must be a positive number")
    return float(value)


def load_config(path: Path) -> dict:
    data = _load_yaml(path)

    model = data.get("model", "claude-opus-5-5")
    if not isinstance(model, str) or not model.strip():
        raise ConfigError(f"{path.name}: 'model' must be a model ID string")

    effort = data.get("effort", "medium")
    if effort not in EFFORT_LEVELS:
        raise ConfigError(f"{path.name}: 'effort' must be one of {sorted(EFFORT_LEVELS)}")

    max_searches = data.get("max_searches", 20)
    if isinstance(max_searches, bool) or not isinstance(max_searches, int) or not 1 <= max_searches <= 100:
        raise ConfigError(f"{path.name}: 'max_searches' must be a whole number from 1 to 100")

    tz_name = data.get("timezone", "America/New_York")
    try:
        tz = ZoneInfo(str(tz_name))
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"{path.name}: unknown timezone {tz_name!r}") from None

    alerts = data.get("alerts")
    if not isinstance(alerts, dict):
        raise ConfigError(f"{path.name}: 'alerts' section is required")
    topics = alerts.get("news_topics", [])
    if not isinstance(topics, list) or not all(isinstance(t, str) and t.strip() for t in topics):
        raise ConfigError(f"{path.name}: alerts.news_topics must be a list of text items")

    return {
        "model": model.strip(),
        "effort": effort,
        "max_searches": max_searches,
        "timezone": tz,
        "alerts": {
            "price_move_pct": _positive_number(alerts, "price_move_pct", path),
            "volume_spike_ratio": _positive_number(alerts, "volume_spike_ratio", path),
            "week52_proximity_pct": _positive_number(alerts, "week52_proximity_pct", path),
            "news_topics": [t.strip() for t in topics],
        },
    }


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a careful equity research assistant who writes a short daily market \
brief for a small group of investors. Use web search to find what happened in \
the last 24 hours. Report only figures and events you found in a source; when \
you could not find a number, say so rather than estimating. Prefer primary \
sources and established financial news outlets. Keep the tone factual and do \
not give buy or sell recommendations.

Link every figure and news item to the page it came from with an inline \
Markdown link right after it, for example: "fell 4.2% ([Reuters](https://...))". \
In the holdings table, link each row's headline. Use only URLs that appeared \
in your search results, exactly as given; never write a URL from memory."""


def build_prompt(holdings: list[dict], cfg: dict, today: dt.date) -> str:
    a = cfg["alerts"]
    lines = []
    for h in holdings:
        line = f"- {h['symbol']} ({h['name']})"
        if h["notes"]:
            line += f" — notes: {h['notes']}"
        lines.append(line)
    topics = ", ".join(a["news_topics"]) or "(none)"

    return f"""\
Today is {today:%A, %B} {today.day}, {today.year}. Write today's market brief for these holdings:

{chr(10).join(lines)}

Flag a holding as an alert when any of these is true:
- its price moved {a['price_move_pct']:g}% or more since the previous close
- its volume is at least {a['volume_spike_ratio']:g}x its recent average
- its price is within {a['week52_proximity_pct']:g}% of its 52-week high or low
- there was news in the last 24 hours about: {topics}

Write the brief in Markdown with exactly this structure, and nothing before the first heading:

{BRIEF_HEADING} — {today:%B} {today.day}, {today.year}

## Alerts
One bullet per triggered alert: symbol, which rule tripped, and the key fact. \
Write "No alerts today." if none tripped.

## Market overview
Two or three sentences on the broad market (major indexes, rates, notable macro news).

## Holdings
A Markdown table with columns: Symbol | Last price | Change | Headline. \
Then a short paragraph per holding with the news that matters, if any.

## What to watch
Upcoming events in the next few days relevant to these holdings (earnings dates, \
economic releases, product events)."""


# --------------------------------------------------------------------------
# Calling Claude
# --------------------------------------------------------------------------

def generate_brief(holdings: list[dict], cfg: dict, today: dt.date) -> tuple[str, list[tuple[str, str]], str]:
    """Return (markdown body, [(title, url), ...] sources, model that answered)."""
    import anthropic  # imported here so --validate and --dry-run need no SDK

    client = anthropic.Anthropic()
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": cfg["max_searches"]}]
    messages = [{"role": "user", "content": build_prompt(holdings, cfg, today)}]
    content: list = []
    model_used = cfg["model"]

    for _ in range(MAX_CONTINUATIONS + 1):
        try:
            with client.beta.messages.stream(
                model=cfg["model"],
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                thinking={"type": "adaptive"},
                output_config={"effort": cfg["effort"]},
                tools=tools,
                messages=messages,
                # If a safety classifier declines, retry on Anthropic's recommended fallback model.
                betas=["server-side-fallback-2026-07-01"],
                extra_body={"fallbacks": "default"},
            ) as stream:
                response = stream.get_final_message()
        except anthropic.AuthenticationError:
            raise BriefError("ANTHROPIC_API_KEY is missing or invalid") from None
        except anthropic.RateLimitError as exc:
            raise BriefError(f"rate limited by the Claude API: {exc}") from None
        except anthropic.APIStatusError as exc:
            raise BriefError(f"Claude API error {exc.status_code}: {exc.message}") from None
        except anthropic.APIConnectionError as exc:
            raise BriefError(f"could not reach the Claude API: {exc}") from None

        model_used = response.model
        content.extend(response.content)
        usage = response.usage
        print(
            f"[{response.model}] stop={response.stop_reason} "
            f"in={usage.input_tokens} out={usage.output_tokens}",
            file=sys.stderr,
        )

        if response.stop_reason == "pause_turn":
            # The server-side search loop paused; resend the turn so it resumes.
            messages = [messages[0], {"role": "assistant", "content": content}]
            continue
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise BriefError(f"the model declined to write the brief (category: {category})")
        if response.stop_reason == "max_tokens":
            raise BriefError("the brief was cut off at max_tokens")
        break
    else:
        raise BriefError(f"web research did not finish after {MAX_CONTINUATIONS} continuations")

    body, sources, unverified = extract_brief(content)
    if not body:
        raise BriefError("the response contained no brief text")
    print(f"sources: {len(sources)} linked", file=sys.stderr)
    for url in unverified:
        print(f"warning: linked URL was not in the search results: {url}", file=sys.stderr)
    return body, sources, model_used


MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def extract_brief(content: list) -> tuple[str, list[tuple[str, str]], list[str]]:
    """Return (brief body, [(title, url), ...] sources, unverified URLs).

    Sources come from API citations first, then from inline Markdown links in
    the brief. A linked URL that never appeared in a search result or citation
    is reported as unverified (only when search results are available to
    check against).
    """
    text_parts, sources, seen_urls = [], [], set()
    known_titles: dict[str, str] = {}

    def add_source(title: str, url: str) -> None:
        if url not in seen_urls:
            seen_urls.add(url)
            sources.append((title or url, url))

    for block in content:
        block_type = getattr(block, "type", None)
        if block_type == "web_search_tool_result":
            results = getattr(block, "content", None)
            if isinstance(results, list):  # an error result is a single object
                for result in results:
                    url = getattr(result, "url", None)
                    if url:
                        known_titles[url] = getattr(result, "title", None) or url
        elif block_type == "text":
            text_parts.append(block.text)
            for citation in getattr(block, "citations", None) or []:
                url = getattr(citation, "url", None)
                if url:
                    known_titles.setdefault(url, getattr(citation, "title", None) or url)
                    add_source(getattr(citation, "title", None), url)

    body = "".join(text_parts).strip()
    # Drop any preamble the model wrote before the brief's heading.
    start = body.find(BRIEF_HEADING)
    if start > 0:
        body = body[start:]

    unverified = []
    for label, url in MD_LINK_RE.findall(body):
        if known_titles and url not in known_titles and url not in unverified:
            unverified.append(url)
        add_source(known_titles.get(url) or label, url)
    return body, sources, unverified


def render_markdown(body: str, sources: list[tuple[str, str]], model: str, generated_at: dt.datetime) -> str:
    parts = [body.rstrip(), ""]
    if sources:
        parts.append("## Sources")
        parts.extend(
            f"{i}. [{title.replace(']', ')').replace('[', '(')}]({url})"
            for i, (title, url) in enumerate(sources, 1)
        )
        parts.append("")
    parts.append(
        f"<sub>Generated {generated_at:%Y-%m-%d %H:%M %Z} by {model} with web search. "
        "Automated summary, not investment advice — verify figures before acting.</sub>"
    )
    return "\n".join(parts) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stocks", type=Path, default=ROOT / "stocks.yaml")
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "briefs")
    parser.add_argument("--date", type=dt.date.fromisoformat, help="brief date (YYYY-MM-DD); default is today")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--validate", action="store_true", help="only validate stocks.yaml and config.yaml")
    mode.add_argument("--dry-run", action="store_true", help="print the prompt instead of calling the API")
    args = parser.parse_args(argv)

    try:
        holdings = load_holdings(args.stocks)
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.validate:
        print(f"OK: {len(holdings)} holdings, config valid ({', '.join(h['symbol'] for h in holdings)})")
        return 0

    now = dt.datetime.now(cfg["timezone"])
    today = args.date or now.date()

    if args.dry_run:
        print(f"model={cfg['model']} effort={cfg['effort']} max_searches={cfg['max_searches']}\n")
        print(SYSTEM_PROMPT, "\n")
        print(build_prompt(holdings, cfg, today))
        return 0

    try:
        body, sources, model_used = generate_brief(holdings, cfg, today)
    except BriefError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / f"{today.isoformat()}.md"
    out_path.write_text(render_markdown(body, sources, model_used, now), encoding="utf-8")
    print(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
