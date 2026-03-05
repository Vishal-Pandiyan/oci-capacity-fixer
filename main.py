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
from dotenv import load_dotenv

# ── Load .env ──────────────────────────────────────────────────────────────────
load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("launch_instance.log"),
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
MAX_RETRIES          = int(os.getenv("MAX_RETRIES",             "0"))  # 0 = infinite

# ── Telegram Config ────────────────────────────────────────────────────────────
TG_BOT_TOKEN         = get_env("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID           = get_env("TELEGRAM_CHAT_ID")
TG_NOTIFY_EVERY      = int(os.getenv("TELEGRAM_NOTIFY_EVERY_N_ATTEMPTS", "10"))


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

def tg_send(message: str, silent: bool = False) -> None:
    """Send a Telegram message. Fails silently to never break the main loop."""
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id":    TG_CHAT_ID,
            "text":       message,
            "parse_mode": "HTML",
            "disable_notification": silent,
        }
        resp = requests.post(url, json=payload, timeout=10)
        if not resp.ok:
            log.warning(f"Telegram send failed: {resp.status_code} {resp.text}")
    except Exception as e:
        log.warning(f"Telegram error (non-fatal): {e}")


def tg_test() -> bool:
    """Verify Telegram credentials on startup. Returns True if OK."""
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getMe"
        resp = requests.get(url, timeout=10)
        if resp.ok:
            bot_name = resp.json().get("result", {}).get("username", "unknown")
            log.info(f"Telegram bot verified: @{bot_name}")
            return True
        else:
            log.warning(f"Telegram bot check failed: {resp.status_code}")
            return False
    except Exception as e:
        log.warning(f"Telegram bot check error: {e}")
        return False


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

    # Validate Telegram
    tg_ok = tg_test()

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
            f"🖥 Shape: <code>{SHAPE}</code>\n"
            f"⚙️ OCPUs: <code>{OCPUS}</code>  RAM: <code>{MEMORY_GB} GB</code>\n"
            f"🌍 Region: <code>{OCI_REGION}</code>\n"
            f"📍 AD: <code>{AVAILABILITY_DOMAIN}</code>\n"
            f"🔁 Retry every <code>{RETRY_INTERVAL}s</code>\n"
            f"⏳ Waiting for capacity...",
            silent=True,
        )

    attempt = 0

    while True:
        attempt += 1
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
                f"🆔 ID: <code>{instance.id}</code>\n"
                f"📛 Name: <code>{instance.display_name}</code>\n"
                f"📊 State: <code>{instance.lifecycle_state}</code>\n"
                f"🌍 Region: <code>{instance.region}</code>\n"
                f"🖥 Shape: <code>{instance.shape}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎉 Done after <b>{attempt}</b> attempt(s)!\n"
                f"⏱ Allow 1-2 min to reach RUNNING state."
            )
            break

        except oci.exceptions.ServiceError as e:
            if e.status == 500 and "Out of host capacity" in str(e.message):
                log.warning(f"[Attempt {attempt}] Out of capacity — retrying in {RETRY_INTERVAL}s...")
                # Notify every N attempts
                if attempt % TG_NOTIFY_EVERY == 0:
                    tg_send(
                        f"⏳ <b>Still trying...</b>\n"
                        f"Attempt <b>{attempt}</b> — out of capacity.\n"
                        f"Retrying every <code>{RETRY_INTERVAL}s</code>.",
                        silent=True,
                    )

            elif e.status == 429:
                backoff = RETRY_INTERVAL * 2
                log.warning(f"[Attempt {attempt}] Rate limited (429) — backing off {backoff}s...")
                tg_send(f"⚠️ Rate limited (429) at attempt {attempt}. Backing off {backoff}s.", silent=True)
                time.sleep(backoff)
                continue

            elif e.status == 400:
                msg = f"Bad request — check your .env values:\n{e.message}"
                log.error(f"[Attempt {attempt}] {msg}")
                tg_send(f"❌ <b>Fatal: Bad Request (400)</b>\n<code>{e.message}</code>")
                sys.exit(1)

            elif e.status == 401:
                log.error(f"[Attempt {attempt}] Auth failed — check OCI credentials in .env")
                tg_send("❌ <b>Fatal: Auth Failed (401)</b>\nCheck OCI credentials in your .env file.")
                sys.exit(1)

            elif e.status == 404:
                log.error(f"[Attempt {attempt}] Resource not found — check OCIDs in .env:\n{e.message}")
                tg_send(f"❌ <b>Fatal: Resource Not Found (404)</b>\n<code>{e.message}</code>")
                sys.exit(1)

            elif "LimitExceeded" in str(e.code):
                log.error(f"[Attempt {attempt}] Limit exceeded — already have a free instance?")
                tg_send("❌ <b>Fatal: Limit Exceeded</b>\nYou may already have a free A1 instance.")
                sys.exit(1)

            else:
                log.warning(f"[Attempt {attempt}] API error ({e.status}): {e.message}")

        except Exception as e:
            log.warning(f"[Attempt {attempt}] Unexpected error: {e}")

        if MAX_RETRIES > 0 and attempt >= MAX_RETRIES:
            log.error(f"Reached max retries ({MAX_RETRIES}). Stopping.")
            tg_send(f"🛑 <b>Stopped</b> after {MAX_RETRIES} attempts. No instance created.")
            sys.exit(1)

        log.info(f"Waiting {RETRY_INTERVAL}s...")
        time.sleep(RETRY_INTERVAL)


if __name__ == "__main__":
    main()