#!/usr/bin/env bash
# /usr/local/bin/detect-camera-content.sh
# Scans a mounted SD card and identifies which camera(s) produced the content.
#
# Detection strategy (layered, most-specific first):
#   1. Directory structure  — canonical folder layouts per manufacturer
#   2. File extensions      — GoPro .LRV/.THM, Canon .CR2/.CR3, Nikon .NEF, etc.
#   3. Filename patterns    — GoPro GX/GP prefixes, Sony DSC_, DJI_ prefixes, etc.
#   4. EXIF metadata        — Make/Model fields via exiftool (if installed)
#
# Writes a JSON summary to <mount_point>/.camera_detect_report.json
# which is read by sdcard-copy.py to determine which camera configs apply.

# NOTE: -e and -u are intentionally omitted.
# -e would cause the script to abort on the first zero-result grep/find (which
#    is expected and normal during detection).
# -u is not needed here and would cause failures when iterating empty arrays.
set -o pipefail

MOUNT_POINT="${1:?Usage: $0 <mount_point>}"
LOG_PREFIX="[camera-detect]"

log()  { echo "$LOG_PREFIX $*"; }
info() { log "INFO  $*"; }
warn() { log "WARN  $*"; }

# Accumulate detected camera types in an associative array (acts as a set)
declare -A DETECTED

# ─────────────────────────────────────────────────────────────────────────────
# 1. DIRECTORY STRUCTURE DETECTION
# ─────────────────────────────────────────────────────────────────────────────
detect_by_directory() {
    info "Scanning directory structure under $MOUNT_POINT ..."

    # GoPro: stores chapters under DCIM/###GOPRO or DCIM/###GoPro
    if find "$MOUNT_POINT/DCIM" -maxdepth 1 -type d -iname '*gopro*' 2>/dev/null | grep -q .; then
        DETECTED["GoPro"]=1
    fi

    # Canon: DCIM/###CANON or DCIM/###EOS or MISC/EOS_DIGITAL
    if find "$MOUNT_POINT/DCIM" -maxdepth 1 -type d \( -iname '*canon*' -o -iname '*eos*' \) 2>/dev/null | grep -q .; then
        DETECTED["Canon DSLR/Mirrorless"]=1
    fi
    if [ -d "$MOUNT_POINT/MISC" ] && ls "$MOUNT_POINT/MISC/" 2>/dev/null | grep -qi 'eos'; then
        DETECTED["Canon DSLR/Mirrorless"]=1
    fi

    # Nikon: DCIM/###NIKON or DCIM/NIKON### or presence of NCFL folder
    if find "$MOUNT_POINT/DCIM" -maxdepth 1 -type d -iname '*nikon*' 2>/dev/null | grep -q .; then
        DETECTED["Nikon DSLR/Mirrorless"]=1
    fi
    if [ -d "$MOUNT_POINT/NCFL" ]; then
        DETECTED["Nikon DSLR/Mirrorless"]=1
    fi

    # Sony: DCIM/###MSDCF or PRIVATE/M4ROOT (cinema/mirrorless)
    if find "$MOUNT_POINT/DCIM" -maxdepth 1 -type d -iname '*msdcf*' 2>/dev/null | grep -q .; then
        DETECTED["Sony Camera"]=1
    fi
    if [ -d "$MOUNT_POINT/PRIVATE/M4ROOT" ]; then
        DETECTED["Sony Cinema/Mirrorless (XAVC)"]=1
    fi

    # Fujifilm: DCIM/###_FUJI
    if find "$MOUNT_POINT/DCIM" -maxdepth 1 -type d -iname '*fuji*' 2>/dev/null | grep -q .; then
        DETECTED["Fujifilm Camera"]=1
    fi

    # Olympus/OM System: DCIM/###OLYMP
    if find "$MOUNT_POINT/DCIM" -maxdepth 1 -type d -iname '*olymp*' 2>/dev/null | grep -q .; then
        DETECTED["Olympus/OM System Camera"]=1
    fi

    # Panasonic/Lumix: PRIVATE/AVCHD
    if [ -d "$MOUNT_POINT/PRIVATE/AVCHD" ]; then
        DETECTED["Panasonic/Lumix (AVCHD)"]=1
    fi

    # DJI Drone: DCIM/DJI_### or DJI_MEDIA
    if find "$MOUNT_POINT/DCIM" -maxdepth 1 -type d -iname 'dji*' 2>/dev/null | grep -q .; then
        DETECTED["DJI Drone"]=1
    fi
    if [ -d "$MOUNT_POINT/DJI_MEDIA" ]; then
        DETECTED["DJI Drone"]=1
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# 2. FILE EXTENSION DETECTION
# ─────────────────────────────────────────────────────────────────────────────
detect_by_extension() {
    info "Scanning file extensions ..."

    declare -A EXTS
    while IFS= read -r -d '' f; do
        ext="${f##*.}"
        EXTS["${ext,,}"]=1
    done < <(find "$MOUNT_POINT" -type f -print0 2>/dev/null)

    # GoPro-specific sidecars
    [[ "${EXTS[lrv]:-}" ]] && DETECTED["GoPro"]=1   # low-res proxy video
    [[ "${EXTS[thm]:-}" ]] && DETECTED["GoPro"]=1   # thumbnail sidecar

    # Canon raw formats
    [[ "${EXTS[cr2]:-}" ]] && DETECTED["Canon DSLR/Mirrorless"]=1
    [[ "${EXTS[cr3]:-}" ]] && DETECTED["Canon DSLR/Mirrorless"]=1
    [[ "${EXTS[crw]:-}" ]] && DETECTED["Canon DSLR/Mirrorless"]=1

    # Nikon raw
    [[ "${EXTS[nef]:-}" ]] && DETECTED["Nikon DSLR/Mirrorless"]=1
    [[ "${EXTS[nrw]:-}" ]] && DETECTED["Nikon DSLR/Mirrorless"]=1

    # Sony raw
    [[ "${EXTS[arw]:-}" ]] && DETECTED["Sony Camera"]=1
    [[ "${EXTS[srf]:-}" ]] && DETECTED["Sony Camera"]=1
    [[ "${EXTS[sr2]:-}" ]] && DETECTED["Sony Camera"]=1

    # Fujifilm raw
    [[ "${EXTS[raf]:-}" ]] && DETECTED["Fujifilm Camera"]=1

    # Olympus raw
    [[ "${EXTS[orf]:-}" ]] && DETECTED["Olympus/OM System Camera"]=1

    # Panasonic raw
    [[ "${EXTS[rw2]:-}" ]] && DETECTED["Panasonic/Lumix Camera"]=1

    # Hasselblad
    [[ "${EXTS[3fr]:-}" ]] && DETECTED["Hasselblad Camera"]=1
    [[ "${EXTS[fff]:-}" ]] && DETECTED["Hasselblad Camera"]=1

    # Phase One
    [[ "${EXTS[iiq]:-}" ]] && DETECTED["Phase One Camera"]=1

    # DNG — generic raw
    [[ "${EXTS[dng]:-}" ]] && DETECTED["DNG Raw (Canon/Leica/Ricoh/GoPro/DJI)"]=1

    # AVCHD / MTS / M2TS — camcorders, Sony, Panasonic
    if [[ "${EXTS[mts]:-}" || "${EXTS[m2ts]:-}" ]]; then
        DETECTED["AVCHD Camcorder/Mirrorless"]=1
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# 3. FILENAME PATTERN DETECTION
# ─────────────────────────────────────────────────────────────────────────────
detect_by_filename() {
    info "Scanning filename patterns ..."

    # GoPro chapter naming: GX010001.MP4 (HEVC) or GP010001.MP4 (AVC)
    if find "$MOUNT_POINT" -type f \( -iname 'GX[0-9]*.MP4' -o -iname 'GP[0-9]*.MP4' \) 2>/dev/null | grep -q .; then
        DETECTED["GoPro"]=1
    fi

    # Sony: DSC_####.JPG / DSC_####.ARW
    if find "$MOUNT_POINT" -type f -iname 'DSC_[0-9]*.JPG' 2>/dev/null | grep -q .; then
        DETECTED["Sony Camera"]=1
    fi

    # Canon: IMG_####.JPG / _MG_####.JPG
    if find "$MOUNT_POINT" -type f \( -iname 'IMG_[0-9]*.JPG' -o -iname '_MG_[0-9]*.JPG' \) 2>/dev/null | grep -q .; then
        DETECTED["Canon DSLR/Mirrorless"]=1
    fi

    # Nikon: _DSC####.JPG (vs Sony DSC_ — note underscore position)
    if find "$MOUNT_POINT" -type f -iname '_DSC[0-9]*.JPG' 2>/dev/null | grep -q .; then
        DETECTED["Nikon DSLR/Mirrorless"]=1
    fi

    # Fujifilm: DSCF####.JPG
    if find "$MOUNT_POINT" -type f -iname 'DSCF[0-9]*.JPG' 2>/dev/null | grep -q .; then
        DETECTED["Fujifilm Camera"]=1
    fi

    # Panasonic: P100####.JPG
    if find "$MOUNT_POINT" -type f -iname 'P[0-9][0-9][0-9][0-9][0-9][0-9][0-9].JPG' 2>/dev/null | grep -q .; then
        DETECTED["Panasonic/Lumix Camera"]=1
    fi

    # DJI: DJI_####.JPG / DJI_####.MP4
    if find "$MOUNT_POINT" -type f \( -iname 'DJI_[0-9]*.JPG' -o -iname 'DJI_[0-9]*.MP4' -o -iname 'DJI_[0-9]*.DNG' \) 2>/dev/null | grep -q .; then
        DETECTED["DJI Drone"]=1
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# 4. EXIF METADATA DETECTION (requires exiftool)
# ─────────────────────────────────────────────────────────────────────────────
detect_by_exif() {
    if ! command -v exiftool &>/dev/null; then
        warn "exiftool not found — skipping EXIF detection."
        warn "Install with: apt install libimage-exiftool-perl"
        return
    fi

    info "Scanning EXIF metadata (sampling up to 20 files) ..."

    mapfile -d '' SAMPLES < <(
        find "$MOUNT_POINT" -type f \( \
            -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.cr2' -o -iname '*.cr3' \
            -o -iname '*.nef' -o -iname '*.arw'  -o -iname '*.raf'  -o -iname '*.rw2' \
            -o -iname '*.mp4' -o -iname '*.mov'  -o -iname '*.avi'  -o -iname '*.mts' \
        \) -print0 2>/dev/null | head -z -n 20
    )

    if [[ ${#SAMPLES[@]} -eq 0 ]]; then
        warn "No recognisable image/video files found for EXIF scan."
        return
    fi

    while IFS= read -r make_model; do
        make_model_lower="${make_model,,}"
        case "$make_model_lower" in
            *gopro*)                           DETECTED["GoPro"]=1 ;;
            *canon*)                           DETECTED["Canon DSLR/Mirrorless"]=1 ;;
            *nikon*)                           DETECTED["Nikon DSLR/Mirrorless"]=1 ;;
            *sony*)                            DETECTED["Sony Camera"]=1 ;;
            *fujifilm*|*fuji*)                 DETECTED["Fujifilm Camera"]=1 ;;
            *olympus*|*om*system*|*om-system*) DETECTED["Olympus/OM System Camera"]=1 ;;
            *panasonic*)                       DETECTED["Panasonic/Lumix Camera"]=1 ;;
            *dji*)                             DETECTED["DJI Drone"]=1 ;;
            *hasselblad*)                      DETECTED["Hasselblad Camera"]=1 ;;
            *phase*one*|*phaseone*)            DETECTED["Phase One Camera"]=1 ;;
            *leica*)                           DETECTED["Leica Camera"]=1 ;;
            *ricoh*)                           DETECTED["Ricoh/Pentax Camera"]=1 ;;
            *pentax*)                          DETECTED["Ricoh/Pentax Camera"]=1 ;;
            *sigma*)                           DETECTED["Sigma Camera"]=1 ;;
        esac
    done < <(
        exiftool -Make -Model -p '$Make $Model' "${SAMPLES[@]}" 2>/dev/null \
            | sort -u
    )
}

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
main() {
    info "======================================================"
    info "Starting camera content detection on: $MOUNT_POINT"
    info "======================================================"

    if ! mountpoint -q "$MOUNT_POINT"; then
        warn "$MOUNT_POINT does not appear to be a mount point. Aborting."
        exit 1
    fi

    detect_by_directory
    detect_by_extension
    detect_by_filename
    detect_by_exif

    info "------------------------------------------------------"
    if [[ ${#DETECTED[@]} -eq 0 ]]; then
        info "RESULT: No recognised camera content detected."
        info "        Card may be empty, corrupted, or from an unsupported device."
    else
        info "RESULT: Detected content from the following camera(s):"
        for camera in "${!DETECTED[@]}"; do
            info "        ✔  $camera"
        done
    fi
    info "======================================================"

    # Write JSON summary for sdcard-copy.py to read
    REPORT_FILE="${MOUNT_POINT}/.camera_detect_report.json"
    {
        echo "{"
        echo "  \"mount_point\": \"$MOUNT_POINT\","
        echo "  \"detected_at\": \"$(date --iso-8601=seconds)\","
        echo "  \"cameras\": ["
        first=1
        for camera in "${!DETECTED[@]}"; do
            [[ $first -eq 1 ]] && first=0 || echo ","
            printf '    "%s"' "$camera"
        done
        echo ""
        echo "  ]"
        echo "}"
    } > "$REPORT_FILE" 2>/dev/null || warn "Could not write report to $REPORT_FILE"

    info "Report written to: $REPORT_FILE"
}

main "$@"
