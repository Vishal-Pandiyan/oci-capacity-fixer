# OCI Always Free Ampere A1 — Instance Launcher

Automatically retries launching an OCI **VM.Standard.A1.Flex** (4 OCPU / 24 GB RAM) Always Free instance until capacity is available. Sends real-time notifications to your Telegram.

> **Auth method:** All credentials are loaded from `.env` — no `~/.oci/config` needed.

---

## How It Works

1. Reads all config from `.env` on startup
2. Validates OCI credentials and Telegram bot
3. Enters an infinite retry loop calling the OCI LaunchInstance API
4. On **"Out of host capacity"** → waits and retries automatically
5. On **success** → sends you a Telegram alert with full instance details and exits
6. On **fatal errors** (bad credentials, wrong OCIDs) → alerts you on Telegram and stops immediately

---

## Prerequisites

- Python 3.8+
- An OCI account (Free Tier)
- A Telegram account

---

## Installation

### 1. Clone or download the files

```bash
git clone https://github.com/Vishal-Pandiyan/oci-capacity-fixer.git
cd oci-capacity-fixer
```

Make sure you have these files in one folder:
```
main.py
.env.example
```

### 2. Install dependencies

```bash
pip install oci python-dotenv requests
```

### 3. Create your `.env` file

```bash
cp .env.example .env
```

Then open `.env` and fill in all the values (see [Environment Variables](#environment-variables) below).

---

## OCI Setup

You need to collect several values from your OCI Console before filling in `.env`.

### OCI API Key (for authentication)

1. Log in to [OCI Console](https://cloud.oracle.com)
2. Click your profile icon (top right) → **User Settings**
3. Go to **API Keys** → **Add API Key**
4. Select **Generate API Key Pair** → download both keys
5. Save the **private key** (`.pem` file) to your machine, e.g. `~/.oci/oci_api_key.pem`
6. After adding, OCI shows a config preview — copy the values:
   - `user` → `OCI_USER_OCID`
   - `tenancy` → `OCI_TENANCY_OCID`
   - `fingerprint` → `OCI_FINGERPRINT`
   - `region` → `OCI_REGION`

### Compartment OCID

1. OCI Console → **Identity & Security** → **Compartments**
2. Click your compartment (or use root = same as your Tenancy OCID)
3. Copy the **OCID** → `OCI_COMPARTMENT_OCID`

### Availability Domain

1. OCI Console → **Compute** → **Instances** → **Create Instance**
2. Note the Availability Domain shown (e.g. `xxxx:AP-MUMBAI-1-AD-1`)
3. Copy it → `OCI_AVAILABILITY_DOMAIN`

> **Tip:** If one AD is always out of capacity, try AD-2 or AD-3 by changing the suffix.

### Subnet OCID

1. OCI Console → **Networking** → **Virtual Cloud Networks**
2. Click your VCN → **Subnets**
3. Click a public subnet → copy the **OCID** → `OCI_SUBNET_OCID`

### Image OCID

1. OCI Console → **Compute** → **Images**
2. Filter by: **Platform Images** + **aarch64** (ARM/Ampere architecture)
3. Pick your OS (e.g. Ubuntu 22.04 Minimal — aarch64)
4. Copy the **OCID** → `OCI_IMAGE_OCID`

> ⚠️ Make sure to pick an **aarch64** image. x86 images will not work on the A1 shape.

### SSH Key

The script injects your public SSH key into the instance so you can log in after creation.

**If you already have a key:**
```bash
ls ~/.ssh/id_rsa.pub   # if this exists, use this path
```

**If you don't have one, create it:**
```bash
ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa -N ""
```

Set the path in `.env`:
```env
OCI_SSH_PUBLIC_KEY_PATH=/home/youruser/.ssh/id_rsa.pub
```

After the instance is running, SSH in with:
```bash
ssh -i ~/.ssh/id_rsa ubuntu@<instance-public-ip>
```

---

## Telegram Bot Setup

### Step 1 — Create the bot

1. Open Telegram → search **@BotFather**
2. Send `/newbot`
3. Enter a display name: e.g. `OCI Instance Watcher`
4. Enter a username (must end in `bot`): e.g. `botname_bot`
5. Copy the token BotFather gives you → `TELEGRAM_BOT_TOKEN`

### Step 2 — Get your Chat ID

1. Open Telegram → search **@userinfobot**
2. Send any message to it
3. It replies with your `Id:` number → copy it → `TELEGRAM_CHAT_ID`

### Step 3 — Send your first message to your bot

Before running the script, open your new bot in Telegram and press **Start** (or send any message). This is required for the bot to be able to message you.

---

## Environment Variables

Copy `.env.example` to `.env` and fill in every value:

```env
# ── OCI Authentication ─────────────────────────────────────
OCI_USER_OCID=ocid1.user.oc1..xxxxxx
OCI_TENANCY_OCID=ocid1.tenancy.oc1..xxxxxx
OCI_FINGERPRINT=xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx
OCI_PRIVATE_KEY_PATH=/home/youruser/.oci/oci_api_key.pem
OCI_REGION=ap-mumbai-1

# ── Instance Config ────────────────────────────────────────
OCI_COMPARTMENT_OCID=ocid1.compartment.oc1..xxxxxx
OCI_AVAILABILITY_DOMAIN=xxxx:AP-MUMBAI-1-AD-1
OCI_SUBNET_OCID=ocid1.subnet.oc1.ap-mumbai-1.xxxxxx
OCI_IMAGE_OCID=ocid1.image.oc1.ap-mumbai-1.xxxxxx
OCI_SSH_PUBLIC_KEY_PATH=/home/youruser/.ssh/id_rsa.pub

# ── Shape Config (Always Free Ampere A1) ───────────────────
OCI_SHAPE=VM.Standard.A1.Flex
OCI_OCPUS=4
OCI_MEMORY_GB=24
OCI_BOOT_VOLUME_GB=50

# ── Instance Naming ────────────────────────────────────────
INSTANCE_DISPLAY_NAME=my-free-ampere-instance

# ── Script Behavior ────────────────────────────────────────
RETRY_INTERVAL_SECONDS=60
MAX_RETRIES=0

# ── Telegram Bot ───────────────────────────────────────────
TELEGRAM_BOT_TOKEN=123456789:AAxxxxxx
TELEGRAM_CHAT_ID=987654321
TELEGRAM_NOTIFY_EVERY_N_ATTEMPTS=10
```

### Variable Reference

| Variable | Required | Description |
|---|---|---|
| `OCI_USER_OCID` | ✅ | Your OCI user OCID |
| `OCI_TENANCY_OCID` | ✅ | Your OCI tenancy OCID |
| `OCI_FINGERPRINT` | ✅ | API key fingerprint |
| `OCI_PRIVATE_KEY_PATH` | ✅ | Path to your OCI private key `.pem` file |
| `OCI_REGION` | ✅ | OCI region identifier (e.g. `ap-mumbai-1`) |
| `OCI_COMPARTMENT_OCID` | ✅ | Target compartment OCID |
| `OCI_AVAILABILITY_DOMAIN` | ✅ | Availability domain (e.g. `xxxx:AP-MUMBAI-1-AD-1`) |
| `OCI_SUBNET_OCID` | ✅ | Public subnet OCID |
| `OCI_IMAGE_OCID` | ✅ | OS image OCID (must be aarch64) |
| `OCI_SSH_PUBLIC_KEY_PATH` | ✅ | Path to your local SSH public key |
| `OCI_SHAPE` | ✅ | Shape name — keep as `VM.Standard.A1.Flex` |
| `OCI_OCPUS` | ✅ | Number of OCPUs — keep as `4` for Always Free |
| `OCI_MEMORY_GB` | ✅ | RAM in GB — keep as `24` for Always Free |
| `OCI_BOOT_VOLUME_GB` | optional | Boot volume size in GB (default: `50`) |
| `INSTANCE_DISPLAY_NAME` | optional | Name shown in OCI Console (default: `free-ampere-instance`) |
| `RETRY_INTERVAL_SECONDS` | optional | Seconds between retries (default: `60`) |
| `MAX_RETRIES` | optional | Max attempts — `0` = retry forever (default: `0`) |
| `TELEGRAM_BOT_TOKEN` | ✅ | Token from @BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | Your Telegram user ID from @userinfobot |
| `TELEGRAM_NOTIFY_EVERY_N_ATTEMPTS` | optional | Progress ping frequency (default: `10`) |

---

## Running the Script

### Basic run

```bash
python main.py
```

### Recommended — run in tmux (keeps running after you close terminal)

```bash
# Install tmux if needed
sudo apt install tmux -y

# Start a named session
tmux new-session -s oci

# Inside tmux, run the script
python main.py

# Detach (script keeps running in background)
# Press:  Ctrl+B  then  D

# Re-attach anytime to check progress
tmux attach -t oci
```

### Auto-start on reboot

```bash
crontab -e
```

Add this line:
```
@reboot sleep 15 && tmux new-session -d -s oci -c /path/to/your/script 'python launch_free_instance.py'
```

---

## Telegram Notifications

| Event | Type | Sound |
|---|---|---|
| Script started | Startup summary | 🔕 Silent |
| Every N failed attempts | Progress ping | 🔕 Silent |
| Rate limited (429) | Backoff warning | 🔕 Silent |
| Bad config / wrong OCIDs | Fatal error details | 🔔 Loud |
| Auth failure | Fatal error | 🔔 Loud |
| ✅ Instance launched | Full instance details | 🔔 Loud |
| Max retries reached | Stopped notification | 🔔 Loud |

**Example success message you'll receive:**
```
✅ Instance Launched!
━━━━━━━━━━━━━━━━━━━━
🆔 ID: ocid1.instance.oc1.ap-mumbai-1.xxxx
📛 Name: my-free-ampere-instance
📊 State: PROVISIONING
🌍 Region: ap-mumbai-1
🖥 Shape: VM.Standard.A1.Flex
━━━━━━━━━━━━━━━━━━━━
🎉 Done after 47 attempt(s)!
⏱ Allow 1-2 min to reach RUNNING state.
```

---

## Error Reference

| Error | Cause | Action |
|---|---|---|
| `401 Auth Failed` | Wrong credentials in `.env` | Check `OCI_USER_OCID`, `OCI_FINGERPRINT`, `OCI_PRIVATE_KEY_PATH` |
| `400 Bad Request` | Invalid OCID or parameter | Check `OCI_SUBNET_OCID`, `OCI_IMAGE_OCID`, `OCI_AVAILABILITY_DOMAIN` |
| `404 Not Found` | OCID points to wrong region or deleted resource | Verify all OCIDs in OCI Console |
| `LimitExceeded` | Already have a free A1 instance | Check OCI Console → Compute → Instances |
| `Out of host capacity` | No A1 capacity right now | Script retries automatically — just wait |
| `429 Rate Limited` | Too many API calls | Script backs off automatically (2× interval) |
| `Telegram send failed` | Wrong token or Chat ID | Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` |

---

## Log File

All output is saved to `launch_instance.log` in the same directory as the script. Useful for reviewing history after detaching from tmux.

```bash
tail -f launch_instance.log   # watch live
cat launch_instance.log       # view full log
```

---

## Tips

- **Try all 3 Availability Domains** if one is always out of capacity. Change `AD-1` to `AD-2` or `AD-3` in `OCI_AVAILABILITY_DOMAIN` and run parallel sessions in separate tmux windows.
- **Don't set `RETRY_INTERVAL_SECONDS` below 60** — OCI may rate-limit your account.
- **Verify your Telegram token** anytime by visiting: `https://api.telegram.org/botYOUR_TOKEN/getMe`
