#!/usr/bin/env bash
# Generate a weekly recap video from archived reward images.
# Creates a card-flip style transition video of all celebration images from the past week.
#
# Usage: ./generate-weekly-recap.sh [days_back]
#   days_back: how many days to include (default: 7)
#
# Requires: ffmpeg, bc
# Output: writes video to rewards/weekly-recap-<date>.mp4 and prints the path

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ARCHIVE_DIR="$SCRIPT_DIR/../rewards"
DAYS_BACK="${1:-7}"
TODAY="$(date +%Y-%m-%d)"
OUTPUT="${ARCHIVE_DIR}/weekly-recap-${TODAY}.mp4"

# Check dependencies
for cmd in ffmpeg bc; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "Error: $cmd is required but not found" >&2
        exit 1
    fi
done

if [ ! -f "$ARCHIVE_DIR/manifest.log" ]; then
    echo "No reward images found. Complete some tasks first!" >&2
    exit 1
fi

# Calculate cutoff timestamp
CUTOFF=$(date -d "${DAYS_BACK} days ago" +%s 2>/dev/null || date -v-"${DAYS_BACK}"d +%s 2>/dev/null)

# Collect images from the past N days (tab-delimited manifest)
IMAGES=()
while IFS=$'\t' read -r timestamp _intensity _title filepath; do
    if [ "$timestamp" -ge "$CUTOFF" ] 2>/dev/null && [ -f "$filepath" ]; then
        IMAGES+=("$filepath")
    fi
done < "$ARCHIVE_DIR/manifest.log"

IMAGE_COUNT=${#IMAGES[@]}

if [ "$IMAGE_COUNT" -eq 0 ]; then
    echo "No reward images from the past $DAYS_BACK days." >&2
    exit 1
fi

echo "Building recap from $IMAGE_COUNT celebration images..."

# Duration per image and transition
DISPLAY_SECS=2.5
TRANSITION_SECS=0.8

if [ "$IMAGE_COUNT" -eq 1 ]; then
    # Single image — just make a short clip
    ffmpeg -y -loop 1 -t 4 -i "${IMAGES[0]}" \
        -vf "scale=1024:1024:force_original_aspect_ratio=decrease,pad=1024:1024:(ow-iw)/2:(oh-ih)/2:black,format=yuv420p" \
        -c:v libx264 -preset medium -crf 23 -pix_fmt yuv420p \
        -movflags +faststart \
        "$OUTPUT" 2>/dev/null
else
    # Multiple images — crossfade transitions (card-flip feel)
    # Build ffmpeg command as an array to avoid eval
    FFMPEG_ARGS=(-y)

    # Add input files
    for img in "${IMAGES[@]}"; do
        TOTAL_PER_IMAGE=$(echo "$DISPLAY_SECS + $TRANSITION_SECS" | bc)
        FFMPEG_ARGS+=(-loop 1 -t "$TOTAL_PER_IMAGE" -i "$img")
    done

    # Build scale filters for all inputs
    SCALE_FILTERS=""
    for i in $(seq 0 $((IMAGE_COUNT - 1))); do
        SCALE_FILTERS="${SCALE_FILTERS}[${i}:v]scale=1024:1024:force_original_aspect_ratio=decrease,pad=1024:1024:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=30[s${i}];"
    done

    # Build xfade chain using scaled inputs
    TRANSITIONS=("fadegrays" "circlecrop" "radial" "hblind" "vuslice" "dissolve")
    XFADE_CHAIN=""
    LAST_STREAM=""

    for i in $(seq 1 $((IMAGE_COUNT - 1))); do
        OFFSET=$(echo "$i * $DISPLAY_SECS" | bc)
        T_IDX=$((i % ${#TRANSITIONS[@]}))
        TRANSITION="${TRANSITIONS[$T_IDX]}"

        if [ "$i" -eq 1 ]; then
            XFADE_CHAIN="${XFADE_CHAIN}[s0][s1]xfade=transition=${TRANSITION}:duration=${TRANSITION_SECS}:offset=${OFFSET}[v1];"
            LAST_STREAM="[v1]"
        else
            PREV=$((i - 1))
            XFADE_CHAIN="${XFADE_CHAIN}[v${PREV}][s${i}]xfade=transition=${TRANSITION}:duration=${TRANSITION_SECS}:offset=${OFFSET}[v${i}];"
            LAST_STREAM="[v${i}]"
        fi
    done

    # Add final fade out
    FINAL_OFFSET=$(echo "$IMAGE_COUNT * $DISPLAY_SECS" | bc)
    FULL_FILTER="${SCALE_FILTERS}${XFADE_CHAIN}${LAST_STREAM}fade=t=out:st=${FINAL_OFFSET}:d=1,format=yuv420p[outv]"

    FFMPEG_ARGS+=(-filter_complex "$FULL_FILTER")
    FFMPEG_ARGS+=(-map "[outv]")
    FFMPEG_ARGS+=(-c:v libx264 -preset medium -crf 23 -pix_fmt yuv420p)
    FFMPEG_ARGS+=(-movflags +faststart)
    FFMPEG_ARGS+=("$OUTPUT")

    ffmpeg "${FFMPEG_ARGS[@]}" 2>/dev/null
fi

echo "$OUTPUT"
echo "Recap includes $IMAGE_COUNT celebrations from the past $DAYS_BACK days."
