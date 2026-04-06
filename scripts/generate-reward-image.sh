#!/usr/bin/env bash
# Generate a celebratory reward image using OpenAI's image generation API.
# Usage: ./generate-reward-image.sh <intensity> [task_title] [streak_count]
#
# Intensity levels: low, medium, high, epic
# Output: writes image to /tmp/reward-<timestamp>.png and prints the path

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- Offline fallback rewards ---
# When image generation is unavailable (API outage, missing key, network error),
# suggest a real-life non-digital reward so the user still gets a celebration.
OFFLINE_REWARDS=(
    "Grab your favorite snack — you earned it!"
    "Treat yourself to a cupcake or some ice cream!"
    "Play your favorite video game for 30 minutes — guilt-free!"
    "Make yourself a fancy coffee or hot chocolate."
    "Take a walk outside and enjoy the fresh air for 15 minutes."
    "Put on your favorite song and have a mini dance party!"
    "Call or text a friend you haven't talked to in a while."
    "Watch an episode of your favorite show — you deserve a break!"
    "Grab a piece of chocolate or your favorite candy."
    "Do some stretches or a quick yoga session — treat your body!"
    "Read a chapter of a book you've been meaning to get to."
    "Order your favorite takeout for dinner tonight."
)

# Print a random offline reward suggestion and write it to a text file.
# Exits 0 so callers know the reward was delivered (just not as an image).
suggest_offline_reward() {
    local reason="$1"
    local count=${#OFFLINE_REWARDS[@]}
    local index=$((RANDOM % count))
    local suggestion="${OFFLINE_REWARDS[$index]}"

    echo "Image generation unavailable ($reason) — here's a real-life reward instead:" >&2
    echo "  🎁 $suggestion" >&2

    # Write the suggestion to a text file so callers can use it
    local fallback_file="/tmp/reward-${TIMESTAMP:-$(date +%s)}-fallback.txt"
    printf '%s\n' "$suggestion" > "$fallback_file"
    echo "$fallback_file"
    exit 0
}

# shellcheck source=scripts/load-env.sh
if ! source "$SCRIPT_DIR/load-env.sh" OPENAI_API_KEY?; then
    suggest_offline_reward "env file unavailable"
fi

if [ -z "${OPENAI_API_KEY:-}" ]; then
    suggest_offline_reward "OPENAI_API_KEY not set"
fi

# Check dependencies
for cmd in python3 curl; do
    if ! command -v "$cmd" &>/dev/null; then
        suggest_offline_reward "$cmd not found"
    fi
done

INTENSITY="${1:-medium}"
TASK_TITLE="${2:-a task}"
STREAK="${3:-0}"
TIMESTAMP="$(date +%s)"
DATE_STR="$(date +%Y-%m-%d)"
TIME_STR="$(date +%H%M%S)"

# Archive directory — persistent collection of all reward images
ARCHIVE_DIR="$SCRIPT_DIR/../rewards"
mkdir -p "$ARCHIVE_DIR"

# Output goes to both /tmp (for immediate use) and archive (for collection)
OUTPUT="/tmp/reward-${TIMESTAMP}.png"
ARCHIVE_FILE="${ARCHIVE_DIR}/${DATE_STR}_${TIME_STR}_${INTENSITY}.png"

# Build a prompt based on intensity and context
build_prompt() {
    local base_style="Digital illustration, vibrant colors, celebratory mood, clean design, no text, no words, no letters, no numbers."

    case "$INTENSITY" in
        low)
            local themes=(
                "A small cheerful bird landing on a branch with a tiny sparkle, soft warm colors"
                "A single firework blooming in a twilight sky, gentle and pretty"
                "A happy cat stretching contentedly in a sunbeam, cozy vibes"
                "A little plant sprouting from soil with a tiny golden glow around it"
                "A paper airplane soaring gracefully through cotton candy clouds"
            )
            ;;
        medium)
            local themes=(
                "A fox doing a victory dance in a meadow full of wildflowers, joyful energy"
                "Colorful confetti exploding from a gift box with sparkles everywhere"
                "A rocket launching through a ring of stars, triumphant and exciting"
                "An otter sliding down a rainbow waterfall, pure joy and fun"
                "A mountain peak at golden hour with aurora borealis, sense of achievement"
            )
            ;;
        high)
            local themes=(
                "A magnificent phoenix rising from golden flames into a starlit sky, powerful and majestic"
                "A dragon made of northern lights soaring over a glowing cityscape, epic achievement"
                "An astronaut planting a flag on a new planet with galaxies swirling behind them"
                "A massive tree of light growing from the ground up through clouds into space"
                "A whale breaching through an ocean of stars and nebulae, awe-inspiring"
            )
            ;;
        epic)
            local themes=(
                "An entire galaxy forming the shape of a crown, supernovae exploding in celebration, cosmic triumph"
                "A titan standing atop a mountain as reality itself celebrates with fractured light and prismatic explosions"
                "The sun and moon aligned in a cosmic high-five with rings of fire and ice, universal celebration"
                "A colossal phoenix made of galaxies rising above a planet, the ultimate achievement"
                "Reality itself folding into a cathedral of light and color, the most epic moment imaginable"
            )
            ;;
        *)
            echo "Unknown intensity: $INTENSITY" >&2
            exit 1
            ;;
    esac

    # Pick a random theme
    local count=${#themes[@]}
    local index=$((RANDOM % count))
    local theme="${themes[$index]}"

    # Add streak flavor for streaks
    if [ "$STREAK" -ge 5 ]; then
        theme="$theme, with a trail of five glowing orbs representing a winning streak"
    elif [ "$STREAK" -ge 3 ]; then
        theme="$theme, with three small stars orbiting nearby"
    fi

    echo "${theme}. ${base_style}"
}

PROMPT=$(build_prompt)

# Choose quality based on intensity
if [ "$INTENSITY" = "epic" ]; then
    QUALITY="high"
else
    QUALITY="auto"
fi

# Generate the image — pass variables via environment to avoid shell injection
# Temporarily allow failure so we can catch network/curl errors
set +e
RESPONSE=$(
    PROMPT="$PROMPT" QUALITY="$QUALITY" \
    python3 -c "
import json, os
payload = json.dumps({
    'model': 'gpt-image-1',
    'prompt': os.environ['PROMPT'],
    'n': 1,
    'size': '1024x1024',
    'quality': os.environ['QUALITY']
})
print(payload)
" | curl -s --max-time 90 -X POST "https://api.openai.com/v1/images/generations" \
    -H "Authorization: Bearer $OPENAI_API_KEY" \
    -H "Content-Type: application/json" \
    -d @-
)
CURL_EXIT=$?
set -e

if [ $CURL_EXIT -ne 0 ] || [ -z "$RESPONSE" ]; then
    suggest_offline_reward "network error or API unreachable"
fi

# Check for errors
ERROR=$(echo "$RESPONSE" | python3 -c "
import json, sys
data = json.load(sys.stdin)
if 'error' in data:
    print(data['error']['message'])
" 2>/dev/null || true)

if [ -n "$ERROR" ]; then
    suggest_offline_reward "API error: $ERROR"
fi

# Extract and save the image (gpt-image-1 returns b64_json by default)
set +e
OUTPUT_PATH="$OUTPUT" python3 -c "
import json, sys, base64, os
data = json.load(sys.stdin)
result = data['data'][0]
out = os.environ['OUTPUT_PATH']
if 'b64_json' in result:
    img_data = base64.b64decode(result['b64_json'])
    with open(out, 'wb') as f:
        f.write(img_data)
elif 'url' in result:
    import urllib.request
    urllib.request.urlretrieve(result['url'], out)
" <<< "$RESPONSE"
EXTRACT_EXIT=$?
set -e

if [ $EXTRACT_EXIT -ne 0 ] || [ ! -s "$OUTPUT" ]; then
    suggest_offline_reward "failed to decode image from API response"
fi

# Copy to archive
cp "$OUTPUT" "$ARCHIVE_FILE"

# Write metadata for the weekly recap (tab-delimited to avoid issues with | in titles)
printf '%s\t%s\t%s\t%s\n' "$TIMESTAMP" "$INTENSITY" "$TASK_TITLE" "$ARCHIVE_FILE" >> "$ARCHIVE_DIR/manifest.log"

echo "$OUTPUT"
