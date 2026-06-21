# EmcurePriceTracker — Command Reference

## Local Mac Commands

| Command | Usage |
|---------|-------|
| `python main.py` | Launch interactive Rich dashboard |
| `python trade.py buy 1693` | Record entry at ₹1693 (auto qty) |
| `python trade.py buy 1693 60` | Record entry with 60 shares |
| `python trade.py sell` | Close trade, show P&L |
| `python trade.py status` | Show live P&L + target levels |
| `./start_bot.sh` | Start WhatsApp bot + ngrok (local dev only) |

---

## Oracle Cloud Server

| Command | Usage |
|---------|-------|
| `ssh -i emcurekey ubuntu@<SERVER_IP>` | SSH into server |
| `sudo systemctl status emcure-bot` | Check bot service status |
| `sudo systemctl status emcure-tracker` | Check alert engine status |
| `sudo systemctl restart emcure-bot` | Restart bot after code change |
| `sudo systemctl restart emcure-tracker` | Restart alert engine |
| `sudo systemctl stop emcure-bot` | Stop bot |
| `tail -f /var/log/emcure/bot.log` | Watch bot activity live |
| `tail -f /var/log/emcure/tracker.log` | Watch alert engine live |
| `tail -f /var/log/emcure/bot.err` | Debug bot errors |

---

## Deployment / Update

| Command | Usage |
|---------|-------|
| `git push origin main` | Push code changes to GitHub |
| `cd /opt/emcure && sudo git pull` | Pull latest code on server |
| `sudo systemctl restart emcure-bot emcure-tracker` | Apply code changes after pull |
| `curl -s https://<YOUR_DOMAIN>/health` | Verify bot is live |

---

## WhatsApp Commands (send to +14155238886)

| Command | Usage |
|---------|-------|
| `BUY 1693` | Record entry at ₹1693 (auto qty from CAPITAL) |
| `BUY 1693 60` | Record entry with 60 shares |
| `SELL` | Close trade, show final P&L |
| `STATUS` | Live P&L + T1/T2/T3/SL progress |
| `HELP` | Show all commands |

---

## Troubleshooting

| Command | Usage |
|---------|-------|
| `sudo iptables -L INPUT -n --line-numbers` | Check firewall rules order |
| `sudo iptables -I INPUT 5 -p tcp --dport 443 -j ACCEPT` | Open port 443 if blocked |
| `sudo iptables -I INPUT 5 -p tcp --dport 80 -j ACCEPT` | Open port 80 if blocked |
| `sudo netfilter-persistent save` | Save iptables rules permanently |
| `sudo nginx -t` | Test nginx config syntax |
| `sudo systemctl reload nginx` | Reload nginx after config change |
| `sudo certbot renew --dry-run` | Test SSL auto-renewal |
| `dig +short <YOUR_DOMAIN>` | Check DNS resolution |

---

## Server Details

| Item | Value |
|------|-------|
| IP | <SERVER_IP> |
| Region | ap-mumbai-1 (Oracle Cloud) |
| OS | Ubuntu 22.04 ARM |
| Webhook | https://<YOUR_DOMAIN>/whatsapp |
| Health | https://<YOUR_DOMAIN>/health |
| App dir | /opt/emcure |
| Logs | /var/log/emcure/ |
| SSH key | emcurekey (in project root, gitignored) |
