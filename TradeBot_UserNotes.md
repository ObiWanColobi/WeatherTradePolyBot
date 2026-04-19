# TradeBotNotes
Commands
venv\Scripts\activate
deactivate

python weather_bot.py
streamlit run ui/weather_dashboard.py --server.port 8501

cswap --switch
cswap --switch-to colby.pearson55@gmail.com
cswap --switch-to dabombsquad55@gmail.com


# Powershell Prompts
ssh root@103.240.146.244
Cheork3355!!!!

systemctl stop weatherbot weather-dashboard

systemctl start weatherbot weather-dashboard
systemctl status weatherbot --no-pager
journalctl -u weatherbot -f

systemctl restart weatherbot weather-dashboard


## How to deploy new code later
Whenever you push to the live branch on GitHub:

cd /opt/weatherbot
git pull
source .venv/bin/activate
pip install -r requirements.txt     # only if requirements changed
sudo systemctl restart weatherbot weather-dashboard
journalctl -u weatherbot -n 50      # confirm healthy restart

### Quick troubleshooting
Problem	- Command

Is the bot alive?	sudo systemctl status weatherbot

Why did it crash?	journalctl -u weatherbot -n 100

Dashboard blank page	Verify Nginx Upgrade headers; sudo nginx -t; sudo systemctl restart weather-dashboard

Can't reach http://103.240.146.244	Check Kamatera firewall for port 80

Out of disk	df -h then sudo journalctl --vacuum-size=200M

Locked out of SSH	Kamatera console → TradeBot0 → Open Console (browser-based root shell)


Pull DB file from Hosted Server
scp root@103.240.146.244:/opt/weatherbot/weather_bot.db "f:\CodeProjects\TestCode1\weather_bot_from_server.db"

## Follow Bot Log
journalctl -u weatherbot -f
## The -f means "follow" (like tail -f) — new log lines stream in as the bot writes them. Ctrl+C detaches you from the log; the bot keeps running in the background regardless.

Handy variants

# Last 100 lines then follow new ones
journalctl -u weatherbot -n 100 -f
journalctl -u weatherbot -n 2000 -f


# Last 100 lines, no follow (just a snapshot)
journalctl -u weatherbot -n 100 --no-pager

# Only since this boot
journalctl -u weatherbot -b

# Filter for a specific word (e.g. errors, orphans)
journalctl -u weatherbot -f | grep -iE "error|orphan|recovery"

# Both services at once
journalctl -u weatherbot -u weather-dashboard -f
Nav inside the log viewer (when not using -f)
When -f is off, journalctl pages the output:

That pulls the last ~5000 lines (~50 pages at ~100 lines/page). Adjust the -n number if you want more or less. 
journalctl -u weatherbot -n 5000 --no-pager

Space or PgDn — next page
b or PgUp — previous page
G — jump to end
g — jump to start
/word — search forward for "word", then n for next match
q — quit
--no-pager skips the pager entirely and dumps straight to the terminal — good when you want to copy-paste output back to me.

# Wallet Addresses
EOA (signing key + gas): 0x48a8D21b02A7EbEd52389878900F911cbD218b46
Proxy (trading capital / USDC): 0x0E5CaC0fc03f5728ddFb3b0B5BeE0cC0Ec67c55d
https://www.ankr.com/rpc









# Todo personal notes
    Maybe we consider a slightly more refined entry strategy. 
        We see trades go sideways mostly because we enter too early (2+ days in advance) Maybe we need to consider only placing trades with 48hrs to close? look at the data we gather and see.

    Should we force the leg to wait until 12hr to close increments? or maybe we just allow it based on edge and instead and then give it a forced cooldown?

Entry strategy
    Ensemble consensus > 70% or < 30%       ← model conviction
    Edge vs market price > min threshold    ← mispricing exists  
    Volume > city-tier threshold            ← liquidity present
    Spread < 5 cents                        ← market is mature
    Price stable (not moving rapidly)       ← price discovery done


Exit strategy
    Model remains high confidence, price moving your way	Hold to resolution — let it pay out
    Ensemble consensus flips significantly (new model run contradicts entry)	Exit — the forecast changed, your edge is gone
    Market price moves hard against you early (>15 cents)	Exit — crowd knows something you don't
    Liquidity dries up (spread widens, volume stops)	Exit — can't get out cleanly later
    Market within 2 hours of resolution	Hold — too late to meaningfully exit, ride it


Ensemble Column
    The ensemble column answers: "of our 71 weather model runs, how many predict YES?"
    7% 5/69 means — 5 out of 69 model members forecast a temperature that satisfies the market condition. 7% is just 5/69 expressed as a percentage.
    The number is 69 not 71 because some members returned null values for that forecast day (a few GFS members occasionally drop out), so the denominator fluctuates slightly.
    How to read it in context:
    7% 5/69 with market price $0.69 → models strongly say NO, market disagrees → edge
    99% 68/69 with market price $0.97 → models strongly say YES, market agrees → no edge, already priced
    26% 18/69 with market price $0.09 → models lean NO but market is pricing it too cheap → mild YES edge
    0% 0/69 → every single model says NO — strongest possible signal
    This is effectively your confidence score. The closer to 0% or 100%, the more conviction. Anything between 30–70% means the models are genuinely uncertain and you should avoid that market regardless of price.



# Local Live Testing — Setup Guide
## Step 1: Python Environment

cd /f/CodeProjects/TestCode1
python -m venv venv
source venv/Scripts/activate   # or: venv\Scripts\activate
pip install -r requirements.txt
Key dependencies: py-clob-client, web3, streamlit, requests, python-dotenv, rich

## Step 2: Configure .env
Your current .env is missing the live trading keys. Add:


TRADING_MODE=live
WALLET_PRIVATE_KEY=<your-polygon-EOA-private-key-hex>
Optional:


WALLET_SIGNATURE_TYPE=0          # 0=EOA (default), 1=POLY_PROXY, 2=GNOSIS_SAFE
WALLET_FUNDER_ADDRESS=           # only for proxy/safe wallets
## Step 3: Fund Your Wallet
Before trading, your Polygon wallet needs:

USDC — trading capital (deposited to Polymarket)
MATIC — gas for on-chain claims (~0.01 MATIC minimum)
## Step 4: On-Chain Allowances (one-time)

python scripts/setup_allowances.py
This approves USDC and CTF token spending for Polymarket's contracts on Polygon. Costs a small amount of MATIC gas.

## Step 5: Configure Discord Alerts (recommended)
Create two Discord channels: #bot-alerts and #bot-trades
In each channel: Server Settings > Integrations > Webhooks > New Webhook — copy the URL
Edit config.py and set:

"discord_webhook_alerts": "https://discordapp.com/api/webhooks/YOUR_ID/YOUR_TOKEN",
"discord_webhook_trades": "https://discordapp.com/api/webhooks/YOUR_ID/YOUR_TOKEN",
Alert routing:

Channel	What fires there
#bot-alerts	CLOB auth failures, circuit breaker trips, low gas, crash recovery
#bot-trades	Trade fills, position closes, daily P&L digest, claim confirmations

## Step 6: (Optional) Email Alerts
In config.py, set:


"risk_email_enabled": True,
"risk_email_smtp_host": "smtp.gmail.com",
"risk_email_smtp_port": 587,
"risk_email_from": "you@gmail.com",
"risk_email_to": "you@gmail.com",
"risk_email_password": "<app-password>",   # NOT your account password
For Gmail, generate an App Password at myaccount.google.com/apppasswords.

## Step 7: Dry Run First

python weather_bot.py --dry-run
This scans markets, evaluates candidates, and logs what it would trade — but places zero orders. Run for a few minutes to verify everything connects.

## Step 8: Start Live Bot
Terminal 1 — Bot:
python weather_bot.py

Terminal 2 — Dashboard (optional):
streamlit run ui/weather_dashboard.py

Dashboard at http://localhost:8501 — shows positions, P&L charts, bot health banner, and notification feed.

## What Happens on Startup
Database auto-initializes (weather_bot.db — SQLite)
Positions reconciled with Polymarket exchange
Crash detection via heartbeat.json (alerts if previous shutdown was unclean)
Calibration catch-up pass runs
Enters 60-second poll loop: exit checks → resolution → risk check → entry scan → extended positions → trader monitor → heartbeat write → daily digest
Key Live Safety Limits (in config.py)
Parameter	Value
live_kelly_max_bet_usdc	$25 per trade
live_risk_daily_loss_limit_pct	5%
live_risk_auto_reset	False (manual reset required)
clob_inter_request_delay	300ms between API calls
Circuit breaker trip	3 consecutive failures → escalating cooldown
Monitoring Checklist
Console — real-time logs with rich formatting
Discord — push alerts for critical/warning events
Dashboard — red banner if bot down >3 min, notification feed, P&L charts
heartbeat.json — external watchdog can poll this file (stale >5 min = crash)