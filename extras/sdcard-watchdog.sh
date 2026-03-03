#!/usr/bin/env bash
# /usr/local/bin/sdcard-watchdog.sh
# Polls a mounted SD card and unmounts it when the card becomes unreadable.
# Launched by sdcard-mount.sh after a successful mount.
#
# This is needed for USB card readers that stay enumerated as a USB device
# even with no card inserted — /dev/sdd and /dev/sdd1 persist because the
# *reader* never disconnects, so the kernel never fires a remove event.
#
# The watchdog uses the same dd/O_DIRECT technique to bypass the page cache
# and actually test hardware presence rather than cached data.
#
# Usage: sdcard-watchdog.sh <kernel_dev> <mount_point>
#   e.g. sdcard-watchdog.sh sdd1 /mnt/media/sdd1

KERNEL_DEV="$1"
MOUNT_POINT="$2"

# Derive the disk device from the partition device:
#   sdd1      → /dev/sdd
#   mmcblk0p1 → /dev/mmcblk0
#   nvme0n1p3 → /dev/nvme0n1
DEVICE="/dev/$(echo "$KERNEL_DEV" | sed 's/p[0-9]*$//' | sed 's/[0-9]*$//')"

LOG="/var/log/sdcard-mount.log"
POLL_INTERVAL=3      # seconds between checks
FAIL_THRESHOLD=3     # consecutive failures before declaring card removed

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [watchdog:$KERNEL_DEV] $*" >> "$LOG"; }

log "Watchdog started. Polling $DEVICE every ${POLL_INTERVAL}s."
log "Mount point: $MOUNT_POINT"

consecutive_failures=0

while true; do
    sleep "$POLL_INTERVAL"

    # Check 1: mount point still in mount table
    # If the card was unmounted by another path, exit cleanly.
    if ! grep -q " $MOUNT_POINT " /proc/mounts 2>/dev/null; then
        log "Mount point gone from /proc/mounts — exiting."
        break
    fi

    # Check 2: try to read one block from the raw disk device.
    # iflag=direct bypasses the page cache so we test actual hardware presence.
    # timeout 2 prevents hanging indefinitely on a stalled USB device.
    if ! timeout 2 /bin/dd \
            if="$DEVICE" of=/dev/null \
            bs=512 count=1 \
            iflag=direct 2>/dev/null; then
        consecutive_failures=$((consecutive_failures + 1))
        log "Read failed (failure $consecutive_failures/$FAIL_THRESHOLD)"

        if [ "$consecutive_failures" -ge "$FAIL_THRESHOLD" ]; then
            log "Threshold reached — card removed. Unmounting $MOUNT_POINT."
            /bin/umount -l "$MOUNT_POINT" 2>/dev/null \
                && log "Unmounted $MOUNT_POINT" \
                || log "ERROR: umount -l failed (may already be unmounted)"
            /bin/rmdir "$MOUNT_POINT" 2>/dev/null || true
            log "Watchdog done."
            break
        fi
    else
        # Reset on any successful read
        if [ "$consecutive_failures" -gt 0 ]; then
            log "Read recovered after $consecutive_failures failure(s) — resetting counter."
        fi
        consecutive_failures=0
    fi
done
