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
from datetime import datetime, timedelta
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


# ── OCI Auth (from .env) ───────────────────────────────────────────────────────
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


# ══════════════════════════════════════════════════════════════════════════════
# SHARED STATE  (read/written by both launcher + bot threads)
# ══════════════════════════════════════════════════════════════════════════════

state = {
    "attempt":      0,
    "paused":       False,
    "stop":         False,
    "last_error":   "None",
    "start_time":   datetime.now(),
    "last_attempt_time": None,
}
state_lock = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM — SEND
# ══════════════════════════════════════════════════════════════════════════════

def tg_send(message: str, silent: bool = False) -> None:
    """Send a Telegram message. Never raises — failures are logged only."""
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id":              TG_CHAT_ID,
            "text":                 message,
            "parse_mode":           "HTML",
            "disable_notification": silent,
        }
        resp = requests.post(url, json=payload, timeout=10)
        if not resp.ok:
            log.warning(f"Telegram send failed: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        log.warning(f"Telegram error (non-fatal): {e}")


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
    tg_send(
        "👋 <b>OCI Instance Launcher Bot</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Available commands:\n\n"
        "📊 /status  — Live attempt count &amp; uptime\n"
        "⚙️ /config  — Current OCI &amp; shape settings\n"
        "📋 /log     — Last 10 log lines\n"
        "⏸ /pause   — Pause retrying\n"
        "▶️ /resume  — Resume after pause\n"
        "🛑 /stop    — Stop the script\n"
        "🏓 /ping    — Check bot is alive"
    )


def handle_status(_):
    with state_lock:
        attempt      = state["attempt"]
        paused       = state["paused"]
        last_error   = state["last_error"]
        last_time    = state["last_attempt_time"]

    status_icon  = "⏸ PAUSED" if paused else "🔄 RUNNING"
    last_str     = last_time.strftime("%H:%M:%S") if last_time else "—"

    tg_send(
        f"📊 <b>Status</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔁 State       : <b>{status_icon}</b>\n"
        f"🔢 Attempts    : <b>{attempt}</b>\n"
        f"⏱ Uptime      : <code>{fmt_uptime()}</code>\n"
        f"🕐 Last try    : <code>{last_str}</code>\n"
        f"⚠️ Last error  : <code>{last_error}</code>\n"
        f"🔁 Retry every : <code>{RETRY_INTERVAL}s</code>"
    )


def handle_config(_):
    tg_send(
        f"⚙️ <b>Current Config</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🌍 Region      : <code>{OCI_REGION}</code>\n"
        f"📍 AD          : <code>{AVAILABILITY_DOMAIN}</code>\n"
        f"🖥 Shape       : <code>{SHAPE}</code>\n"
        f"⚙️ OCPUs       : <code>{OCPUS}</code>\n"
        f"🧠 Memory      : <code>{MEMORY_GB} GB</code>\n"
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
        if state["paused"]:
            tg_send("⏸ Already paused. Use /resume to continue.")
            return
        state["paused"] = True
    log.info("Bot command: PAUSE received")
    tg_send("⏸ <b>Paused.</b> Retrying is suspended.\nUse /resume to continue.")


def handle_resume(_):
    with state_lock:
        if not state["paused"]:
            tg_send("▶️ Already running. Use /pause to pause.")
            return
        state["paused"] = False
    log.info("Bot command: RESUME received")
    tg_send("▶️ <b>Resumed.</b> Retrying has restarted.")


def handle_stop(_):
    tg_send("🛑 <b>Stop command received.</b>\nShutting down the launcher...")
    log.info("Bot command: STOP received — shutting down")
    with state_lock:
        state["stop"] = True


def handle_ping(_):
    tg_send(f"🏓 <b>Pong!</b>  Bot is alive.\nUptime: <code>{fmt_uptime()}</code>")


# Map command text → handler function
COMMANDS = {
    "/start":  handle_start,
    "/status": handle_status,
    "/config": handle_config,
    "/log":    handle_log,
    "/pause":  handle_pause,
    "/resume": handle_resume,
    "/stop":   handle_stop,
    "/ping":   handle_ping,
}


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM — POLLING THREAD
# ══════════════════════════════════════════════════════════════════════════════

def bot_polling_thread():
    """
    Runs in a background thread.
    Polls Telegram for new messages and dispatches commands.
    Only accepts messages from TG_CHAT_ID for security.
    """
    offset = None
    log.info("Telegram bot polling started.")

    while True:
        with state_lock:
            if state["stop"]:
                break
        try:
            params = {"timeout": 20, "allowed_updates": ["message"]}
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
                msg    = update.get("message", {})
                chat   = str(msg.get("chat", {}).get("id", ""))
                text   = msg.get("text", "").strip().lower().split("@")[0]

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
    return oci.core.models.LaunchInstanceDetails(
        availability_domain = AVAILABILITY_DOMAIN,
        compartment_id      = COMPARTMENT_OCID,
        shape               = SHAPE,
        shape_config        = oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus         = OCPUS,
            memory_in_gbs = MEMORY_GB,
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

    # Validate OCI config
    config = build_oci_config()
    try:
        oci.config.validate_config(config)
    except oci.exceptions.InvalidConfig as e:
        log.error(f"Invalid OCI config: {e}")
        sys.exit(1)

    ssh_key        = read_ssh_key(SSH_PUBLIC_KEY_PATH)
    compute_client = oci.core.ComputeClient(config)
    launch_details = build_launch_details(ssh_key)

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
        # Check stop flag
        with state_lock:
            if state["stop"]:
                log.info("Stop flag set — exiting.")
                break

        # Check pause flag
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
            with state_lock:
                state["stop"] = True
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
        time.sleep(RETRY_INTERVAL)


if __name__ == "__main__":
    main()