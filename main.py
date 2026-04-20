"""

OCI Always Free Ampere A1 Instance Launcher
============================================
Auth   → fully from .env (no ~/.oci/config needed)
Config → all instance + Telegram values from .env

"""

import oci
import time
import os
import sys
import logging
import requests
import threading
from datetime import datetime
from dotenv import load_dotenv

# ── Load .env ──────────────────────────────────────────────────────────────────
load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────
log_lines = []   # in-memory buffer for /log command (last 50 lines)

class BufferHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        log_lines.append(msg)
        if len(log_lines) > 50:
            log_lines.pop(0)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("launch_instance.log"),
        BufferHandler(),
    ],
)
log = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────
def get_env(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        log.error(f"Missing required .env variable: {key}")
        sys.exit(1)
    return value


# ── OCI Auth (from .env — no ~/.oci/config needed) ────────────────────────────
OCI_USER_OCID        = get_env("OCI_USER_OCID")
OCI_TENANCY_OCID     = get_env("OCI_TENANCY_OCID")
OCI_FINGERPRINT      = get_env("OCI_FINGERPRINT")
OCI_PRIVATE_KEY_PATH = get_env("OCI_PRIVATE_KEY_PATH")
OCI_REGION           = get_env("OCI_REGION")

# ── Instance Config ────────────────────────────────────────────────────────────
COMPARTMENT_OCID     = get_env("OCI_COMPARTMENT_OCID")
AVAILABILITY_DOMAIN  = get_env("OCI_AVAILABILITY_DOMAIN")
SUBNET_OCID          = get_env("OCI_SUBNET_OCID")
IMAGE_OCID           = get_env("OCI_IMAGE_OCID")
SSH_PUBLIC_KEY_PATH  = get_env("OCI_SSH_PUBLIC_KEY_PATH")

SHAPE                = os.getenv("OCI_SHAPE",              "VM.Standard.A1.Flex")
OCPUS                = float(os.getenv("OCI_OCPUS",        "4"))
MEMORY_GB            = float(os.getenv("OCI_MEMORY_GB",    "24"))
BOOT_VOLUME_GB       = int(os.getenv("OCI_BOOT_VOLUME_GB", "50"))

INSTANCE_NAME        = os.getenv("INSTANCE_DISPLAY_NAME",      "free-ampere-instance")
RETRY_INTERVAL       = int(os.getenv("RETRY_INTERVAL_SECONDS", "60"))
MAX_RETRIES          = int(os.getenv("MAX_RETRIES",             "0"))

# ── Telegram Config ────────────────────────────────────────────────────────────
TG_BOT_TOKEN         = get_env("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID           = get_env("TELEGRAM_CHAT_ID")
TG_NOTIFY_EVERY      = int(os.getenv("TELEGRAM_NOTIFY_EVERY_N_ATTEMPTS", "10"))

# ── OCI Always Free A1 RAM mapping per OCPU count ─────────────────────────────
# Source: OCI Always Free limits — 4 OCPU / 24 GB total, 6 GB per OCPU
A1_RAM_MAP = {1: 6.0, 2: 12.0, 3: 18.0, 4: 24.0}


# ══════════════════════════════════════════════════════════════════════════════
# SHARED STATE  (read/written by both launcher + bot threads)
# ══════════════════════════════════════════════════════════════════════════════

state = {
    "attempt":           0,
    "paused":            False,
    "stopped":           False,   # soft stop — process stays alive, loop idles
    "last_error":        "None",
    "start_time":        datetime.now(),
    "last_attempt_time": None,
    # Live-editable shape config (can be changed via /settings)
    "ocpus":             OCPUS,
    "memory_gb":         MEMORY_GB,
    # Tracks the /settings message id for in-place editing
    "settings_msg_id":   None,
    # Pending settings selection before confirm
    "pending_ocpus":     None,
}
state_lock = threading.Lock()

# Event used to wake the main loop instantly when /start is called after /stop
resume_event = threading.Event()


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM — SEND / EDIT / CALLBACK
# ══════════════════════════════════════════════════════════════════════════════

def tg_send(message: str, silent: bool = False, reply_markup: dict = None) -> int | None:
    """Send a Telegram message. Returns message_id on success, None on failure."""
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id":              TG_CHAT_ID,
            "text":                 message,
            "parse_mode":           "HTML",
            "disable_notification": silent,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        resp = requests.post(url, json=payload, timeout=10)
        if resp.ok:
            return resp.json().get("result", {}).get("message_id")
        log.warning(f"Telegram send failed: {resp.status_code} {resp.text[:100]}")
        return None
    except Exception as e:
        log.warning(f"Telegram error (non-fatal): {e}")
        return None


def tg_edit(message_id: int, text: str, reply_markup: dict = None) -> None:
    """Edit an existing Telegram message in-place."""
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/editMessageText"
        payload = {
            "chat_id":    TG_CHAT_ID,
            "message_id": message_id,
            "text":       text,
            "parse_mode": "HTML",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        resp = requests.post(url, json=payload, timeout=10)
        if not resp.ok:
            log.warning(f"Telegram edit failed: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        log.warning(f"Telegram edit error (non-fatal): {e}")


def tg_answer_callback(callback_query_id: str, text: str = "") -> None:
    """Acknowledge a callback query to stop the loading spinner on the button."""
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/answerCallbackQuery"
        requests.post(url, json={"callback_query_id": callback_query_id, "text": text}, timeout=10)
    except Exception as e:
        log.warning(f"Telegram answerCallbackQuery error (non-fatal): {e}")


def tg_test() -> bool:
    """Verify Telegram token on startup."""
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getMe", timeout=10
        )
        if resp.ok:
            bot_name = resp.json().get("result", {}).get("username", "unknown")
            log.info(f"Telegram bot verified: @{bot_name}")
            return True
        log.warning(f"Telegram bot check failed: {resp.status_code}")
        return False
    except Exception as e:
        log.warning(f"Telegram bot check error: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM — COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

def fmt_uptime() -> str:
    delta = datetime.now() - state["start_time"]
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s   = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


def handle_start(_):
    with state_lock:
        is_stopped = state["stopped"]
        is_paused  = state["paused"]

    if is_stopped:
        # Script was soft-stopped — reset and resume
        with state_lock:
            state["stopped"]    = False
            state["paused"]     = False
            state["attempt"]    = 0
            state["last_error"] = "None"
            state["start_time"] = datetime.now()
            state["last_attempt_time"] = None
        resume_event.set()   # wake the idle loop in main
        log.info("Bot command: START received — resuming from stopped state")
        tg_send(
            "▶️ <b>Launcher Restarted!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Attempt counter reset. Retrying for instance now.\n"
            f"⚙️ OCPUs: <code>{state['ocpus']}</code>  "
            f"RAM: <code>{state['memory_gb']} GB</code>"
        )
    elif is_paused:
        # Already running but paused — warn
        tg_send(
            "⚠️ <b>Launcher is paused, not stopped.</b>\n"
            "Use /resume to continue retrying.\n"
            "Use /stop first if you want a full restart."
        )
    else:
        # Already running normally — show command list
        tg_send(
            "👋 <b>OCI Instance Launcher Bot</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Launcher is already running. Available commands:\n\n"
            "📊 /status   — Live attempt count &amp; uptime\n"
            "⚙️ /config   — Current OCI &amp; shape settings\n"
            "🔧 /settings — Change OCPU &amp; RAM via buttons\n"
            "📋 /log      — Last 10 log lines\n"
            "⏸ /pause    — Pause retrying\n"
            "▶️ /resume   — Resume after pause\n"
            "🛑 /stop     — Soft stop (use /start to restart)\n"
            "🏓 /ping     — Check bot is alive"
        )


def handle_status(_):
    with state_lock:
        attempt    = state["attempt"]
        paused     = state["paused"]
        stopped    = state["stopped"]
        last_error = state["last_error"]
        last_time  = state["last_attempt_time"]
        ocpus      = state["ocpus"]
        memory_gb  = state["memory_gb"]

    if stopped:
        status_icon = "🛑 STOPPED"
    elif paused:
        status_icon = "⏸ PAUSED"
    else:
        status_icon = "🔄 RUNNING"

    last_str = last_time.strftime("%H:%M:%S") if last_time else "—"

    tg_send(
        f"📊 <b>Status</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔁 State       : <b>{status_icon}</b>\n"
        f"🔢 Attempts    : <b>{attempt}</b>\n"
        f"⏱ Uptime      : <code>{fmt_uptime()}</code>\n"
        f"🕐 Last try    : <code>{last_str}</code>\n"
        f"⚠️ Last error  : <code>{last_error}</code>\n"
        f"⚙️ OCPUs       : <code>{ocpus}</code>  RAM: <code>{memory_gb} GB</code>\n"
        f"🔁 Retry every : <code>{RETRY_INTERVAL}s</code>"
    )


def handle_config(_):
    with state_lock:
        ocpus     = state["ocpus"]
        memory_gb = state["memory_gb"]

    tg_send(
        f"⚙️ <b>Current Config</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🌍 Region      : <code>{OCI_REGION}</code>\n"
        f"📍 AD          : <code>{AVAILABILITY_DOMAIN}</code>\n"
        f"🖥 Shape       : <code>{SHAPE}</code>\n"
        f"⚙️ OCPUs       : <code>{ocpus}</code>\n"
        f"🧠 Memory      : <code>{memory_gb} GB</code>\n"
        f"💾 Boot Vol    : <code>{BOOT_VOLUME_GB} GB</code>\n"
        f"📛 Name        : <code>{INSTANCE_NAME}</code>\n"
        f"🔁 Interval    : <code>{RETRY_INTERVAL}s</code>\n"
        f"🔔 Notify every: <code>{TG_NOTIFY_EVERY} attempts</code>"
    )


def handle_log(_):
    last_lines = log_lines[-10:] if len(log_lines) >= 10 else log_lines
    if not last_lines:
        tg_send("📋 No log lines yet.")
        return
    log_text = "\n".join(last_lines)
    tg_send(f"📋 <b>Last {len(last_lines)} log lines:</b>\n<pre>{log_text}</pre>")


def handle_pause(_):
    with state_lock:
        if state["stopped"]:
            tg_send("🛑 Launcher is stopped. Use /start to restart it first.")
            return
        if state["paused"]:
            tg_send("⏸ Already paused. Use /resume to continue.")
            return
        state["paused"] = True
    log.info("Bot command: PAUSE received")
    tg_send("⏸ <b>Paused.</b> Retrying is suspended.\nUse /resume to continue.")


def handle_resume(_):
    with state_lock:
        if state["stopped"]:
            tg_send("🛑 Launcher is stopped. Use /start to restart it.")
            return
        if not state["paused"]:
            tg_send("▶️ Already running. Use /pause to pause.")
            return
        state["paused"] = False
    log.info("Bot command: RESUME received")
    tg_send("▶️ <b>Resumed.</b> Retrying has restarted.")


def handle_stop(_):
    with state_lock:
        if state["stopped"]:
            tg_send("🛑 Already stopped. Use /start to restart.")
            return
        state["stopped"] = True
        state["paused"]  = False   # clear pause so idle loop doesn't double-block
    log.info("Bot command: STOP received — soft stop, process stays alive")
    tg_send(
        "🛑 <b>Launcher stopped.</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "The script is now idle. Use /start to restart retrying.\n"
        "Process remains alive — no need to restart manually."
    )


def handle_ping(_):
    tg_send(f"🏓 <b>Pong!</b>  Bot is alive.\nUptime: <code>{fmt_uptime()}</code>")


def _settings_text(selected_ocpus: int | None) -> str:
    """Build the /settings message text showing current and pending selection."""
    with state_lock:
        current_ocpus     = state["ocpus"]
        current_memory_gb = state["memory_gb"]

    lines = [
        "🔧 <b>Settings — OCPU &amp; RAM</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Current : <code>{current_ocpus} OCPU</code> / <code>{current_memory_gb} GB RAM</code>",
        "",
        "Select OCPU count (RAM is auto-calculated):",
    ]
    if selected_ocpus is not None:
        ram = A1_RAM_MAP[selected_ocpus]
        lines.append(f"\n✅ Selected: <b>{selected_ocpus} OCPU / {ram} GB RAM</b>")
        lines.append("Press <b>Confirm</b> to apply.")
    else:
        lines.append("\nTap a button below to choose:")

    return "\n".join(lines)


def _settings_keyboard(selected_ocpus: int | None) -> dict:
    """Build the inline keyboard for /settings."""
    ocpu_row = []
    for n in [1, 2, 3, 4]:
        ram   = A1_RAM_MAP[n]
        label = f"{'✅ ' if n == selected_ocpus else ''}{n} OCPU / {ram:.0f} GB"
        ocpu_row.append({"text": label, "callback_data": f"settings_ocpu_{n}"})

    keyboard = [ocpu_row]  # all 4 buttons on one row

    if selected_ocpus is not None:
        # Show confirm button only after a selection is made
        keyboard.append([{"text": "✅ Confirm", "callback_data": "settings_confirm"}])

    return {"inline_keyboard": keyboard}


def handle_settings(_):
    with state_lock:
        state["pending_ocpus"] = None   # clear any previous pending selection

    text     = _settings_text(None)
    keyboard = _settings_keyboard(None)
    msg_id   = tg_send(text, reply_markup=keyboard)

    with state_lock:
        state["settings_msg_id"] = msg_id

    log.info("Bot command: SETTINGS sent with inline keyboard")


def handle_callback(callback_query: dict) -> None:
    """Handle all inline keyboard button presses from /settings."""
    cq_id = callback_query.get("id")
    data  = callback_query.get("data", "")

    # ── OCPU selection button pressed ─────────────────────────────────────────
    if data.startswith("settings_ocpu_"):
        selected = int(data.split("_")[-1])
        with state_lock:
            state["pending_ocpus"] = selected
            msg_id = state["settings_msg_id"]

        tg_answer_callback(cq_id, f"Selected {selected} OCPU / {A1_RAM_MAP[selected]:.0f} GB")

        if msg_id:
            tg_edit(
                msg_id,
                _settings_text(selected),
                reply_markup=_settings_keyboard(selected),
            )

    # ── Confirm button pressed ─────────────────────────────────────────────────
    elif data == "settings_confirm":
        with state_lock:
            pending = state["pending_ocpus"]
            msg_id  = state["settings_msg_id"]

        if pending is None:
            tg_answer_callback(cq_id, "Nothing selected yet.")
            return

        new_ram = A1_RAM_MAP[pending]

        with state_lock:
            state["ocpus"]          = float(pending)
            state["memory_gb"]      = new_ram
            state["pending_ocpus"]  = None
            state["settings_msg_id"] = None

        tg_answer_callback(cq_id, f"Applied: {pending} OCPU / {new_ram:.0f} GB")
        log.info(f"Settings updated via bot: {pending} OCPU / {new_ram} GB RAM")

        # Edit the settings message to show final applied state
        if msg_id:
            tg_edit(
                msg_id,
                f"✅ <b>Settings Applied</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⚙️ OCPUs  : <code>{pending}</code>\n"
                f"🧠 Memory : <code>{new_ram:.0f} GB</code>\n\n"
                f"New values will be used on the next launch attempt.",
            )


# Map text command → handler function
COMMANDS = {
    "/start":    handle_start,
    "/status":   handle_status,
    "/config":   handle_config,
    "/settings": handle_settings,
    "/log":      handle_log,
    "/pause":    handle_pause,
    "/resume":   handle_resume,
    "/stop":     handle_stop,
    "/ping":     handle_ping,
}


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM — POLLING THREAD
# ══════════════════════════════════════════════════════════════════════════════

def bot_polling_thread():
    """
    Runs in a background thread.
    Polls Telegram for new messages and dispatches commands + callback queries.
    Only accepts updates from TG_CHAT_ID for security.
    """
    log.info("Telegram bot polling started.")

    # ── Skip all pending/old updates on startup ────────────────────────────────
    # Without this, old commands (like /stop) sitting in Telegram's queue
    # would be replayed every time the script restarts, causing instant shutdown.
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates",
            params={"offset": -1},   # fetch only the very last update
            timeout=10,
        )
        if resp.ok:
            results = resp.json().get("result", [])
            if results:
                # Set offset ahead of all existing updates so none get processed
                offset = results[-1]["update_id"] + 1
                log.info(f"Skipped {len(results)} pending Telegram update(s) from previous session.")
            else:
                offset = None
        else:
            offset = None
    except Exception as e:
        log.warning(f"Could not flush old Telegram updates (non-fatal): {e}")
        offset = None

    while True:
        try:
            params = {"timeout": 20, "allowed_updates": ["message", "callback_query"]}
            if offset:
                params["offset"] = offset

            resp = requests.get(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates",
                params=params,
                timeout=25,
            )
            if not resp.ok:
                time.sleep(5)
                continue

            updates = resp.json().get("result", [])
            for update in updates:
                offset = update["update_id"] + 1

                # ── Handle inline keyboard callback queries ────────────────────
                if "callback_query" in update:
                    cq   = update["callback_query"]
                    chat = str(cq.get("message", {}).get("chat", {}).get("id", ""))
                    if chat != str(TG_CHAT_ID):
                        log.warning(f"Ignored callback from unauthorized chat: {chat}")
                        continue
                    handle_callback(cq)
                    continue

                # ── Handle regular text commands ───────────────────────────────
                msg  = update.get("message", {})
                chat = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "").strip().lower().split("@")[0]

                # Security: ignore messages from other chats
                if chat != str(TG_CHAT_ID):
                    log.warning(f"Ignored message from unauthorized chat: {chat}")
                    continue

                if text in COMMANDS:
                    log.info(f"Bot command received: {text}")
                    COMMANDS[text](msg)
                elif text:
                    tg_send(
                        f"❓ Unknown command: <code>{text}</code>\n"
                        "Use /start to see available commands."
                    )

        except Exception as e:
            log.warning(f"Bot polling error (non-fatal): {e}")
            time.sleep(5)


# ══════════════════════════════════════════════════════════════════════════════
# OCI — PRE-LAUNCH VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

def validate_oci_values(config: dict) -> bool:
    """
    Validates all OCI config values via live API calls before starting the retry loop.
    Returns True if all checks pass, False if any fail.
    Logs a clear error for each failure.
    """
    log.info("Validating OCI values via API...")
    errors = []

    try:
        # ── 1. Auth — confirm the API key actually works ───────────────────────
        identity_client = oci.identity.IdentityClient(config)
        identity_client.get_user(OCI_USER_OCID)
        log.info("  ✅ Auth (user OCID + API key)")
    except oci.exceptions.ServiceError as e:
        errors.append(f"Auth failed ({e.status}): {e.message}")
    except Exception as e:
        errors.append(f"Auth check error: {e}")

    try:
        # ── 2. Compartment ────────────────────────────────────────────────────
        identity_client = oci.identity.IdentityClient(config)
        identity_client.get_compartment(COMPARTMENT_OCID)
        log.info("  ✅ Compartment OCID")
    except oci.exceptions.ServiceError as e:
        errors.append(f"Compartment invalid ({e.status}): {e.message}")
    except Exception as e:
        errors.append(f"Compartment check error: {e}")

    try:
        # ── 3. Availability Domain — check it exists in this region ───────────
        # list_availability_domains belongs to IdentityClient, not ComputeClient
        identity_client_ad = oci.identity.IdentityClient(config)
        ads      = identity_client_ad.list_availability_domains(COMPARTMENT_OCID).data
        ad_names = [ad.name for ad in ads]
        if AVAILABILITY_DOMAIN not in ad_names:
            errors.append(
                f"Availability Domain '{AVAILABILITY_DOMAIN}' not found in region '{OCI_REGION}'.\n"
                f"  Valid ADs: {', '.join(ad_names)}"
            )
        else:
            log.info(f"  ✅ Availability Domain ({AVAILABILITY_DOMAIN})")
    except Exception as e:
        errors.append(f"Availability Domain check error: {e}")

    try:
        # ── 4. Subnet ─────────────────────────────────────────────────────────
        vnc_client = oci.core.VirtualNetworkClient(config)
        subnet     = vnc_client.get_subnet(SUBNET_OCID).data
        # Warn if subnet is private (no public IP possible)
        if not subnet.prohibit_public_ip_on_vnic:
            log.info(f"  ✅ Subnet OCID ({subnet.display_name})")
        else:
            errors.append(
                f"Subnet '{subnet.display_name}' prohibits public IPs. "
                "Use a public subnet for internet-accessible instances."
            )
    except oci.exceptions.ServiceError as e:
        errors.append(f"Subnet invalid ({e.status}): {e.message}")
    except Exception as e:
        errors.append(f"Subnet check error: {e}")

    try:
        # ── 5. Image ──────────────────────────────────────────────────────────
        compute_client = oci.core.ComputeClient(config)
        image          = compute_client.get_image(IMAGE_OCID).data
        # OCI SDK Image model has no .architecture field — check operating_system
        # to warn if it looks like a non-ARM image (x86_64 OS names contain "x86")
        os_name = (image.operating_system or "").lower()
        if "x86" in os_name:
            errors.append(
                f"Image '{image.display_name}' appears to be x86-based (OS: {image.operating_system}). "
                "VM.Standard.A1.Flex requires an aarch64 image."
            )
        else:
            log.info(f"  ✅ Image OCID ({image.display_name} — {image.operating_system} {image.operating_system_version})")
    except oci.exceptions.ServiceError as e:
        errors.append(f"Image invalid ({e.status}): {e.message}")
    except Exception as e:
        errors.append(f"Image check error: {e}")

    try:
        # ── 6. SSH public key file ─────────────────────────────────────────────
        if not os.path.isfile(SSH_PUBLIC_KEY_PATH):
            errors.append(f"SSH public key file not found: {SSH_PUBLIC_KEY_PATH}")
        else:
            with open(SSH_PUBLIC_KEY_PATH) as f:
                content = f.read().strip()
            if not content.startswith("ssh-"):
                errors.append(f"SSH key at '{SSH_PUBLIC_KEY_PATH}' does not look like a valid public key.")
            else:
                log.info(f"  ✅ SSH public key ({SSH_PUBLIC_KEY_PATH})")
    except Exception as e:
        errors.append(f"SSH key check error: {e}")

    if errors:
        log.error("=" * 60)
        log.error("OCI VALIDATION FAILED — fix these issues before retrying:")
        for i, err in enumerate(errors, 1):
            log.error(f"  {i}. {err}")
        log.error("=" * 60)
        return False

    log.info("All OCI values validated successfully.")
    return True


# ══════════════════════════════════════════════════════════════════════════════
# OCI HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def read_ssh_key(path: str) -> str:
    if not os.path.isfile(path):
        log.error(f"SSH public key not found: {path}")
        sys.exit(1)
    with open(path, "r") as f:
        return f.read().strip()


def build_oci_config() -> dict:
    return {
        "user":        OCI_USER_OCID,
        "tenancy":     OCI_TENANCY_OCID,
        "fingerprint": OCI_FINGERPRINT,
        "key_file":    OCI_PRIVATE_KEY_PATH,
        "region":      OCI_REGION,
    }


def build_launch_details(ssh_key: str) -> oci.core.models.LaunchInstanceDetails:
    # Always reads from state so /settings changes take effect immediately
    with state_lock:
        ocpus     = state["ocpus"]
        memory_gb = state["memory_gb"]

    return oci.core.models.LaunchInstanceDetails(
        availability_domain = AVAILABILITY_DOMAIN,
        compartment_id      = COMPARTMENT_OCID,
        shape               = SHAPE,
        shape_config        = oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus         = ocpus,
            memory_in_gbs = memory_gb,
        ),
        source_details      = oci.core.models.InstanceSourceViaImageDetails(
            source_type             = "image",
            image_id                = IMAGE_OCID,
            boot_volume_size_in_gbs = BOOT_VOLUME_GB,
        ),
        create_vnic_details = oci.core.models.CreateVnicDetails(
            subnet_id                 = SUBNET_OCID,
            assign_public_ip          = True,
            assign_private_dns_record = True,
        ),
        metadata            = {"ssh_authorized_keys": ssh_key},
        display_name        = INSTANCE_NAME,
        agent_config        = oci.core.models.LaunchInstanceAgentConfigDetails(
            is_monitoring_disabled = False,
            is_management_disabled = False,
        ),
        availability_config = oci.core.models.LaunchInstanceAvailabilityConfigDetails(
            recovery_action = "RESTORE_INSTANCE",
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    log.info("=" * 60)
    log.info("OCI Always Free Ampere A1 — Instance Launcher")
    log.info(f"  Auth        : .env (no ~/.oci/config)")
    log.info(f"  Region      : {OCI_REGION}")
    log.info(f"  Shape       : {SHAPE}  ({OCPUS} OCPU / {MEMORY_GB} GB RAM)")
    log.info(f"  Boot volume : {BOOT_VOLUME_GB} GB")
    log.info(f"  AD          : {AVAILABILITY_DOMAIN}")
    log.info(f"  Retry every : {RETRY_INTERVAL}s  |  Max: {'∞' if MAX_RETRIES == 0 else MAX_RETRIES}")
    log.info(f"  Telegram    : notify every {TG_NOTIFY_EVERY} attempts")
    log.info("=" * 60)

    # Validate Telegram + start polling thread
    tg_ok = tg_test()
    if tg_ok:
        poller = threading.Thread(target=bot_polling_thread, daemon=True)
        poller.start()

    # Validate OCI config structure
    config = build_oci_config()
    try:
        oci.config.validate_config(config)
    except oci.exceptions.InvalidConfig as e:
        log.error(f"Invalid OCI config: {e}")
        sys.exit(1)

    # ── Pre-launch validation — verify all OCI values are real and accessible ──
    if not validate_oci_values(config):
        tg_send(
            "❌ <b>OCI Validation Failed</b>\n"
            "One or more config values are invalid.\n"
            "Check the console log for details.\n"
            "Fix your .env and restart the script."
        )
        sys.exit(1)

    ssh_key        = read_ssh_key(SSH_PUBLIC_KEY_PATH)
    compute_client = oci.core.ComputeClient(config)

    # Startup notification
    if tg_ok:
        tg_send(
            f"🚀 <b>OCI Launcher Started</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🖥 Shape  : <code>{SHAPE}</code>\n"
            f"⚙️ OCPUs  : <code>{OCPUS}</code>  RAM: <code>{MEMORY_GB} GB</code>\n"
            f"🌍 Region : <code>{OCI_REGION}</code>\n"
            f"📍 AD     : <code>{AVAILABILITY_DOMAIN}</code>\n"
            f"🔁 Retry every <code>{RETRY_INTERVAL}s</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💬 Send /start to see bot commands.",
            silent=True,
        )

    attempt = 0

    while True:
        try:
            # ── Check soft-stop flag — idle here until /start is called ───────
            with state_lock:
                is_stopped = state["stopped"]
            if is_stopped:
                log.info("Launcher stopped — idling. Send /start via Telegram to resume.")
                resume_event.clear()
                resume_event.wait()   # blocks until handle_start() calls resume_event.set()
                # Reset attempt counter and rebuild launch details after restart
                attempt = 0
                log.info("Launcher resumed by /start command.")
                continue

            # ── Check pause flag ───────────────────────────────────────────────
            with state_lock:
                is_paused = state["paused"]
            if is_paused:
                log.info("Paused — waiting...")
                time.sleep(5)
                continue

            attempt += 1
            with state_lock:
                state["attempt"]           = attempt
                state["last_attempt_time"] = datetime.now()

            # Rebuild launch details each attempt so /settings changes take effect
            launch_details = build_launch_details(ssh_key)

            log.info(f"[Attempt {attempt}] Requesting instance...")

            try:
                response = compute_client.launch_instance(launch_instance_details=launch_details)
                instance = response.data

                log.info("=" * 60)
                log.info("✅  SUCCESS! Instance launched.")
                log.info(f"   Instance ID  : {instance.id}")
                log.info(f"   Display Name : {instance.display_name}")
                log.info(f"   Lifecycle    : {instance.lifecycle_state}")
                log.info(f"   Region       : {instance.region}")
                log.info(f"   Shape        : {instance.shape}")
                log.info("   Allow 1-2 min to reach RUNNING state.")
                log.info("=" * 60)

                tg_send(
                    f"✅ <b>Instance Launched!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🆔 ID    : <code>{instance.id}</code>\n"
                    f"📛 Name  : <code>{instance.display_name}</code>\n"
                    f"📊 State : <code>{instance.lifecycle_state}</code>\n"
                    f"🌍 Region: <code>{instance.region}</code>\n"
                    f"🖥 Shape : <code>{instance.shape}</code>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎉 Done after <b>{attempt}</b> attempt(s)!\n"
                    f"⏱ Uptime: <code>{fmt_uptime()}</code>\n"
                    f"Allow 1-2 min to reach RUNNING state."
                )
                # Soft stop after success — process stays alive
                with state_lock:
                    state["stopped"] = True
                break

            except oci.exceptions.ServiceError as e:
                if e.status == 500 and "Out of host capacity" in str(e.message):
                    with state_lock:
                        state["last_error"] = "Out of host capacity"
                    log.warning(f"[Attempt {attempt}] Out of capacity — retrying in {RETRY_INTERVAL}s...")
                    if attempt % TG_NOTIFY_EVERY == 0:
                        tg_send(
                            f"⏳ <b>Still trying...</b>\n"
                            f"Attempt <b>{attempt}</b> — out of capacity.\n"
                            f"Uptime: <code>{fmt_uptime()}</code>\n"
                            f"Use /status for details.",
                            silent=True,
                        )

                elif e.status == 429:
                    backoff = RETRY_INTERVAL * 2
                    with state_lock:
                        state["last_error"] = "Rate limited (429)"
                    log.warning(f"[Attempt {attempt}] Rate limited — backing off {backoff}s...")
                    tg_send(f"⚠️ Rate limited (429) at attempt {attempt}. Backing off {backoff}s.", silent=True)
                    time.sleep(backoff)
                    continue

                elif e.status == 400:
                    log.error(f"[Attempt {attempt}] Bad request: {e.message}")
                    tg_send(f"❌ <b>Fatal: Bad Request (400)</b>\n<code>{e.message}</code>")
                    sys.exit(1)

                elif e.status == 401:
                    log.error(f"[Attempt {attempt}] Auth failed — check OCI credentials in .env")
                    tg_send("❌ <b>Fatal: Auth Failed (401)</b>\nCheck OCI credentials in your .env file.")
                    sys.exit(1)

                elif e.status == 404:
                    log.error(f"[Attempt {attempt}] Resource not found: {e.message}")
                    tg_send(f"❌ <b>Fatal: Resource Not Found (404)</b>\n<code>{e.message}</code>")
                    sys.exit(1)

                elif "LimitExceeded" in str(e.code):
                    log.error(f"[Attempt {attempt}] Limit exceeded.")
                    tg_send("❌ <b>Fatal: Limit Exceeded</b>\nYou may already have a free A1 instance.")
                    sys.exit(1)

                else:
                    with state_lock:
                        state["last_error"] = f"API {e.status}: {e.message[:60]}"
                    log.warning(f"[Attempt {attempt}] API error ({e.status}): {e.message}")

            except Exception as e:
                with state_lock:
                    state["last_error"] = str(e)[:80]
                log.warning(f"[Attempt {attempt}] Unexpected error: {e}")

            if MAX_RETRIES > 0 and attempt >= MAX_RETRIES:
                log.error(f"Reached max retries ({MAX_RETRIES}). Stopping.")
                tg_send(f"🛑 <b>Stopped</b> after {MAX_RETRIES} attempts. No instance created.")
                sys.exit(1)

            log.info(f"Waiting {RETRY_INTERVAL}s...")
            try:
                time.sleep(RETRY_INTERVAL)
            except KeyboardInterrupt:
                log.info("Keyboard interrupt received — exiting cleanly.")
                sys.exit(0)

        except KeyboardInterrupt:
            log.info("Keyboard interrupt received — exiting cleanly.")
            sys.exit(0)


if __name__ == "__main__":
    main()