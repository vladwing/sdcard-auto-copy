#!/usr/bin/env bash
# /usr/local/bin/sdcard-mount.sh
# Called by udev: sdcard-mount.sh [mount|unmount] <kernel_device>
#
# On mount: mounts the SD card, then calls the camera-detection script.
# On unmount: cleanly unmounts and removes the mount point.

set -uo pipefail
set -x
ACTION="$1"
KERNEL_DEV="$2"
DEVICE="/dev/${KERNEL_DEV}"
MOUNT_BASE="/mnt/media"
MOUNT_POINT="${MOUNT_BASE}/${KERNEL_DEV}"
LOG="/var/log/sdcard-mount.log"
COPY_SCRIPT="/usr/local/bin/sdcard-copy.py"

log() {
    echo "[$(/bin/date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"
}

do_mount() {
    log "Device appeared: $DEVICE"
    
    # Wait for udev to finish processing the device before we touch it
    udevadm settle --timeout=10 --exit-if-exists="$DEVICE"

    if ! [ -b "$DEVICE" ]; then
        log "ERROR: $DEVICE is not a block device. Aborting."
        exit 1
    fi

    mkdir -p "$MOUNT_POINT"

    FS_TYPE=$(blkid -o value -s TYPE "$DEVICE" 2>/dev/null || echo "")
    log "Detected filesystem: ${FS_TYPE:-unknown}"

    mount_device() {
        # Wraps mount and logs stderr so failures are never silent
        local cmd=("$@")
        local err
        err=$("${cmd[@]}" 2>&1)
        local rc=$?
        if [ $rc -ne 0 ]; then
            log "ERROR: mount failed (exit $rc): $err"
            rmdir "$MOUNT_POINT" 2>/dev/null || true
            exit 1
        fi
    }

    case "$FS_TYPE" in
        vfat)
            mount_device mount -t vfat \
                -o uid=1000,gid=1000,umask=022,noatime \
                "$DEVICE" "$MOUNT_POINT"
            ;;

        exfat)
            # Try native kernel exfat driver first (kernel ≥ 5.7 + exfatprogs)
            # Fall back to exfat-fuse (older kernels / exfat-utils package)
            log "Attempting native exfat mount..."
            ERR=$(mount -t exfat \
                -o uid=1000,gid=1000,umask=022,noatime \
                "$DEVICE" "$MOUNT_POINT" 2>&1)
            if [ $? -ne 0 ]; then
                log "Native exfat failed ($ERR), trying fuse..."
                ERR2=$(mount.exfat-fuse \
                    -o uid=1000,gid=1000,umask=022,noatime \
                    "$DEVICE" "$MOUNT_POINT" 2>&1)
                if [ $? -ne 0 ]; then
                    log "ERROR: Both exfat mount methods failed."
                    log "  native : $ERR"
                    log "  fuse   : $ERR2"
                    log "Fix: apt install exfatprogs   (native, recommended)"
                    log "  or: apt install exfat-fuse  (fuse fallback)"
                    rmdir "$MOUNT_POINT" 2>/dev/null || true
                    exit 1
                fi
                log "Mounted via exfat-fuse."
            else
                log "Mounted via native exfat driver."
            fi
            ;;

        ntfs)
            mount_device mount -t ntfs-3g \
                -o uid=1000,gid=1000,umask=022,noatime \
                "$DEVICE" "$MOUNT_POINT"
            ;;

        ext4|ext3|ext2)
            mount_device mount -t "$FS_TYPE" \
                -o noatime \
                "$DEVICE" "$MOUNT_POINT"
            ;;

        "")
            log "ERROR: Could not determine filesystem type for $DEVICE."
            log "       Run 'blkid $DEVICE' manually to investigate."
            rmdir "$MOUNT_POINT" 2>/dev/null || true
            exit 1
            ;;

        *)
            log "WARNING: Unrecognised filesystem '$FS_TYPE', attempting generic mount."
            mount_device mount "$DEVICE" "$MOUNT_POINT"
            ;;
    esac

    log "Mounted $DEVICE at $MOUNT_POINT (fs: $FS_TYPE)"

    if [ -x "$COPY_SCRIPT" ]; then
        systemd-run \
            --system --no-block --collect \
            --description="SD copy ${KERNEL_DEV}" \
            "$COPY_SCRIPT" "$MOUNT_POINT" "$KERNEL_DEV"
        log "Copy job launched."
        log "Copy script launched as independent systemd unit."
    else
        log "WARNING: Copy script not found or not executable: $COPY_SCRIPT"
    fi
}

do_unmount() {
    log "Device removed: $KERNEL_DEV"

    if /bin/umount -l "$MOUNT_POINT" 2>/dev/null; then
        log "Unmounted $MOUNT_POINT"
    elif grep -q " $MOUNT_POINT " /proc/mounts 2>/dev/null; then
        # Still appears in mount table but umount failed — log it
        log "ERROR: umount -l failed but $MOUNT_POINT still in /proc/mounts"
    else
        log "$MOUNT_POINT was already unmounted."
    fi

    /bin/rmdir "$MOUNT_POINT" 2>/dev/null || true
}

case "$ACTION" in
    mount)   do_mount   ;;
    unmount) do_unmount ;;
    *)
        log "ERROR: Unknown action '$ACTION'"
        exit 1
        ;;
esac
