# REMOTE ACCESS — Steve's Mac (Bend, Oregon)

Written session 8 (2026-05-16) ahead of travel. Two paths documented:
**Tailscale (recommended)** and **direct SSH (fallback / local-net testing)**.

No credentials, passwords, keys, or tokens in this file — only connection
methods and the commands to discover the live values.

---

## 0. One-time prerequisites on the Mac

Run these once before leaving. None of them need to be redone unless macOS
reinstall or you change networks.

### Enable Remote Login (SSH server)

System Settings → General → Sharing → Remote Login: **ON**.

Restrict to specific users (recommended): "Only these users" → add
`stevenemery`. Confirms on the same screen.

Verify from a terminal:

```bash
sudo systemsetup -getremotelogin
# Expected: Remote Login: On
```

### Generate an SSH key on the laptop you'll use while traveling

On the **client** machine (the laptop you're taking, NOT the Mac):

```bash
ssh-keygen -t ed25519 -C "travel-laptop"
# Accept default path (~/.ssh/id_ed25519), set a passphrase
```

Copy the public key to the Mac (run this on the laptop while still on the
home network):

```bash
ssh-copy-id stevenemery@<mac-local-ip>
```

Test that key-only login works:

```bash
ssh -o PasswordAuthentication=no stevenemery@<mac-local-ip>
```

If this prompts for a password, the key copy didn't take — debug before
leaving.

### Disable password SSH (recommended once keys work)

On the Mac, edit `/etc/ssh/sshd_config.d/100-no-passwords.conf`:

```
PasswordAuthentication no
ChallengeResponseAuthentication no
```

Then:

```bash
sudo launchctl unload /System/Library/LaunchDaemons/ssh.plist
sudo launchctl load   /System/Library/LaunchDaemons/ssh.plist
```

Re-verify key auth still works *before* closing the original SSH session.

---

## 1. Tailscale (recommended path)

Zero-config mesh VPN. The Mac and every device you add show up to each
other as if on the same LAN — no port forwarding, no public-internet
exposure, works from any wifi (hotel, coffee shop, plane).

### Install on the Mac

```bash
brew install --cask tailscale
open -a Tailscale
# Sign in with Google account (use emery.stevenr@gmail.com)
# Click "Connect"
```

Get the Mac's Tailscale name and IP:

```bash
tailscale status
# Look for the line ending in "stevens-macbook-pro" (or whatever the
# Mac's hostname is). Two formats appear:
#   100.x.y.z         stevens-macbook-pro    stevenemery@  macOS  -
#   stevens-macbook-pro.<tailnet>.ts.net     (MagicDNS name)
```

Record both — either works as the host argument for SSH.

### Install on the travel laptop

Same `brew install --cask tailscale` (or Tailscale download for your OS),
sign in with the **same Google account**. Two devices on one tailnet =
they can reach each other.

### Connect

From anywhere on the planet:

```bash
ssh stevenemery@stevens-macbook-pro.<tailnet>.ts.net
# or:
ssh stevenemery@100.x.y.z
```

If MagicDNS is enabled (default), the short name `stevens-macbook-pro`
also resolves directly.

### Keep-alive while traveling

The Mac must stay awake for SSH to reach it. Two options:

1. **System Settings → Battery → Options → Prevent automatic sleeping
   when the display is off: ON** (only effective while plugged in).
2. Run `caffeinate` in a detached session before leaving:
   ```bash
   nohup caffeinate -di > /dev/null 2>&1 &
   ```
   Kills automatic sleep until reboot.

Sleep ≠ shutdown. Tailscale also reconnects after wake, but a Mac in
deep sleep won't answer SSH.

---

## 2. Direct SSH (fallback / local-network testing)

Use this to verify SSH works at all before relying on Tailscale, or as a
no-VPN fallback if Tailscale is down. NOT recommended as the primary
remote path because it requires exposing port 22 to the public internet.

### Discover the Mac's local IP and hostname

On the Mac:

```bash
# Local IP on the current network (en0 = wifi, en1 = ethernet)
ipconfig getifaddr en0

# Hostname (also resolvable as <hostname>.local on the same LAN via mDNS)
scutil --get LocalHostName

# External IP as seen by the public internet (from current network)
curl -s ifconfig.me ; echo
```

Record all three. Expected:
- Local IP: `192.168.x.y` (changes if you change networks)
- LocalHostName: e.g. `stevens-macbook-pro` (resolves to `.local` on LAN)
- External IP: residential ISP IP (changes; not useful without DDNS)

### Test from the same network (do this BEFORE leaving)

From another device on the same wifi:

```bash
ssh stevenemery@<local-ip>
# OR
ssh stevenemery@<localhostname>.local
```

If this works, SSH server is configured correctly. If not, fix before
relying on any remote path.

### Public access (NOT recommended; document only)

To make port 22 reachable from outside the home network, two things are
required:

1. **Port forward on the router**: forward external port 22 (or a less
   common port like 2222 → safer) to the Mac's local IP, port 22.
   Bend ISP residential routers vary — check the router admin UI.
2. **Dynamic DNS**: residential ISPs rotate the external IP. Use a DDNS
   service (DuckDNS, no-ip) to get a stable hostname that always points
   to the current IP. Run a DDNS updater script on the Mac.

Public SSH is a constant brute-force target. If using this path:
- Disable password auth (see §0)
- Use a non-standard port
- Consider `fail2ban` (`brew install fail2ban` + config)
- Audit `/var/log/system.log | grep sshd` periodically

This is exactly the friction Tailscale eliminates — use §1 instead.

---

## 3. Verifying the trading stack is reachable

Once SSH connects, these are the first things to check:

```bash
# Dashboard (Ctrl+C to exit, q to quit)
python3 ~/Documents/shadow_dashboard.py

# T6 cycle log — should show a new line every ~30 min
tail -20 ~/Documents/terminal6_data/paper_trader.log

# Daemon liveness — all should be TTY=??, etime increasing across calls
ps -ax -o pid,tty,etime,command | grep terminal[67]_ | grep -v grep

# Scheduler heartbeat — JSON file mtime should be < 30 sec old
stat ~/Documents/scheduler_status.json
```

If any of these fail, the relevant restart commands live in
`HANDOFF_2026-05-16_session7.md` §3 (engine states) and
`redeploy_t6_all.sh`.

---

## 4. Troubleshooting

| Symptom | Most likely cause |
| --- | --- |
| `ssh: connect to host ... port 22: Operation timed out` | Mac is asleep or off network. Wake it (someone at home) or wait until next sync. |
| `Permission denied (publickey)` | Key not authorized on Mac. SSH back in over local network with password (if still enabled) and re-run `ssh-copy-id`. |
| Tailscale `tailscale status` says "Logged out" | Sign back in via the menu bar app on the Mac. |
| `tailscale ping <name>` works but `ssh` hangs | Firewall on Mac blocking 22. System Settings → Network → Firewall → allow `sshd-keygen-wrapper`. |
| Trading scripts run but daemons silent | Run `bash ~/Documents/redeploy_t6_all.sh` (only T6 has one — T7 daemons relaunch manually per session 7 handoff). |

---

## 5. Emergency: kill all trading from remote

If something goes catastrophically wrong and you need to halt trading
immediately:

```bash
# Stop all T6 + T7 daemons (won't restart until you re-run redeploy)
pkill -f terminal6_mlb_
pkill -f terminal7_

# Stop the scheduler (no more puller jobs, no more daily email)
kill $(cat ~/Documents/scheduler.pid)

# Verify nothing trading-related is still running
ps -ax | grep -E "terminal[67]_|portfolio_scheduler" | grep -v grep
```

This freezes the portfolio at its current open positions. Settlement
reconciler is also killed, so any positions still open will need to be
closed manually by running:

```bash
python3 ~/Documents/terminal6_mlb_settlement_reconciler.py --once
```

after you bring everything back up.
