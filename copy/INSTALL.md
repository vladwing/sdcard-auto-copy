# sdcard-copy — installation & integration notes

## Files to deploy

| Source file          | Deploy to                                   | Mode  |
|----------------------|---------------------------------------------|-------|
| sdcard-copy.py       | /usr/local/bin/sdcard-copy.py               | 0755  |
| config.toml          | /etc/sdcard-copy/config.toml                | 0640  |


## Dependencies

Python 3.11+ (tomllib is in stdlib):
    No additional packages required.

Python 3.9 / 3.10:
    pip install tomli


## Directory bootstrap (run once)

Save the following as /etc/tmpfiles.d/sdcard-copy.conf and apply it:

    d /etc/sdcard-copy          0755 root root -
    d /var/lib/sdcard-copy      0755 root root -
    d /var/log/camera-detection 0755 root root -

    systemd-tmpfiles --create

This ensures all required directories exist before the first card is inserted,
even after a fresh OS install.


## Integrating with sdcard-mount.sh

In do_mount(), after the camera detection script is launched, add:

    # Launch copy script in its own systemd scope so it survives the
    # parent scope exiting and has full access to mount(2) and I/O.
    systemd-run \
        --system \
        --no-block \
        --collect \
        --description="SD card copy ${KERNEL_DEV}" \
        /usr/local/bin/sdcard-copy.py "${MOUNT_POINT}" "${KERNEL_DEV}" \
        >> "${LOG}" 2>&1

The copy script must start AFTER detect-camera-content.sh has written its
report.  Since both are launched with --no-block they race.  The safest fix is
to run detection synchronously (without &) and only then launch the copy:

    # Detection — run synchronously so its report is ready before copy starts
    if [ -x "$DETECT_SCRIPT" ]; then
        systemd-run \
            --system \
            --no-block \
            --collect \
            --wait \                            # <-- wait for completion
            --description="SD detect ${KERNEL_DEV}" \
            /bin/bash -c "\"$DETECT_SCRIPT\" \"$MOUNT_POINT\" >> \"$LOG\" 2>&1"
        log "Detection complete."
    fi

    # Copy — launched asynchronously; runs for as long as needed
    systemd-run \
        --system \
        --no-block \
        --collect \
        --description="SD copy ${KERNEL_DEV}" \
        /usr/local/bin/sdcard-copy.py "$MOUNT_POINT" "$KERNEL_DEV"
    log "Copy job launched."

Note: --wait is supported in systemd-run since systemd 232.
Check your version with: systemd-run --version


## Removing the sdcard-watchdog.sh when using sdcard-copy.py

sdcard-copy.py contains its own removal detector (the RemovalDetector class)
that uses the same dd/O_DIRECT technique as sdcard-watchdog.sh.  Running both
in parallel is harmless but redundant.  You can disable the watchdog launch in
do_mount() if the copy script is active, or keep it as a safety net for cards
where no camera config is found (in which case the copy script exits early and
the watchdog takes over unmounting duties).


## Monitoring

Watch the copy log live:
    tail -f /var/log/sdcard-copy.log

List active copy jobs:
    systemctl list-units 'run-*.scope'

Inspect a specific copy job's journal output:
    journalctl -f _SYSTEMD_UNIT=run-XXXXX.scope

Inspect saved card state (replace UUID):
    cat /var/lib/sdcard-copy/<uuid>.state.json | python3 -m json.tool

Count already-copied files for a card:
    python3 -c "
    import json, sys
    with open(sys.argv[1]) as f:
        s = json.load(f)
    print(len(s['copied_files']), 'files copied')
    " /var/lib/sdcard-copy/<uuid>.state.json

Delete state to force a full re-copy:
    rm /var/lib/sdcard-copy/<uuid>.state.json


## Webhook payload examples

card_inserted:
{
  "event": "card_inserted",
  "timestamp": "2024-11-30T14:05:22",
  "card_uuid": "4A21-9F3B",
  "card_label": "GOPRO",
  "device": "/dev/sdd1",
  "mount_point": "/mnt/media/sdd1",
  "kernel_dev": "sdd1",
  "cameras_detected": ["GoPro"]
}

copy_progress:
{
  "event": "copy_progress",
  "timestamp": "2024-11-30T14:06:10",
  "card_uuid": "4A21-9F3B",
  "camera": "GoPro",
  "files_done": 12,
  "files_total": 47,
  "bytes_done": 3145728000,
  "bytes_total": 18253611008,
  "percent": 17.2,
  "eta_seconds": 423
}

card_removed:
{
  "event": "card_removed",
  "timestamp": "2024-11-30T14:09:55",
  "card_uuid": "4A21-9F3B",
  "copy_completed": false
}
