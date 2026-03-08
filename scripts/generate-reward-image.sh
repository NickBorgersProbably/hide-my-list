#!/usr/bin/env bash
# Generate a celebratory reward image using OpenAI's image generation API.
# Usage: ./generate-reward-image.sh <intensity> [task_title] [streak_count]
#
# Intensity levels: low, medium, high, epic
# Output: writes image to /tmp/reward-<timestamp>.png and prints the path

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/../.env"

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
            # Gentle, warm acknowledgment
            local themes=(
                "A small cheerful bird landing on a branch with a tiny sparkle, soft warm colors"
                "A single firework blooming in a twilight sky, gentle and pretty"
                "A happy cat stretching contentedly in a sunbeam, cozy vibes"
                "A little plant sprouting from soil with a tiny golden glow around it"
                "A paper airplane soaring gracefully through cotton candy clouds"
            )
            ;;
        medium)
            # Enthusiastic celebration
            local themes=(
                "A fox doing a victory dance in a meadow full of wildflowers, joyful energy"
                "Colorful confetti exploding from a gift box with sparkles everywhere"
                "A rocket launching through a ring of stars, triumphant and exciting"
                "An otter sliding down a rainbow waterfall, pure joy and fun"
                "A mountain peak at golden hour with aurora borealis, sense of achievement"
            )
            ;;
        high)
            # Major accomplishment
            local themes=(
                "A magnificent phoenix rising from golden flames into a starlit sky, powerful and majestic"
                "A dragon made of northern lights soaring over a glowing cityscape, epic achievement"
                "An astronaut planting a flag on a new planet with galaxies swirling behind them"
                "A massive tree of light growing from the ground up through clouds into space"
                "A whale breaching through an ocean of stars and nebulae, awe-inspiring"
            )
            ;;
        epic)
            # Maximum celebration
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

# Choose model and size based on intensity
if [ "$INTENSITY" = "epic" ]; then
    MODEL="gpt-image-1"
    SIZE="1024x1024"
    QUALITY="high"
else
    MODEL="gpt-image-1"
    SIZE="1024x1024"
    QUALITY="auto"
fi

# Generate the image
RESPONSE=$(curl -s -X POST "https://api.openai.com/v1/images/generations" \
    -H "Authorization: Bearer $OPENAI_API_KEY" \
    -H "Content-Type: application/json" \
    -d "$(python3 -c "
import json
print(json.dumps({
    'model': '$MODEL',
    'prompt': '''$PROMPT''',
    'n': 1,
    'size': '$SIZE',
    'quality': '$QUALITY'
}))
")")

# Check for errors
ERROR=$(echo "$RESPONSE" | python3 -c "
import json, sys
data = json.load(sys.stdin)
if 'error' in data:
    print(data['error']['message'])
" 2>/dev/null || true)

if [ -n "$ERROR" ]; then
    echo "Error generating image: $ERROR" >&2
    exit 1
fi

# Extract and save the image (gpt-image-1 returns b64_json by default)
echo "$RESPONSE" | python3 -c "
import json, sys, base64
data = json.load(sys.stdin)
result = data['data'][0]
if 'b64_json' in result:
    img_data = base64.b64decode(result['b64_json'])
    with open('$OUTPUT', 'wb') as f:
        f.write(img_data)
elif 'url' in result:
    import urllib.request
    urllib.request.urlretrieve(result['url'], '$OUTPUT')
"

# Copy to archive
cp "$OUTPUT" "$ARCHIVE_FILE"

# Write metadata for the weekly recap
echo "${TIMESTAMP}|${INTENSITY}|${TASK_TITLE}|${ARCHIVE_FILE}" >> "$ARCHIVE_DIR/manifest.log"

echo "$OUTPUT"
