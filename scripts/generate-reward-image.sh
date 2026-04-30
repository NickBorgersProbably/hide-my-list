#!/usr/bin/env bash
# Generate a celebratory reward image using OpenAI's image generation API.
# Usage: ./generate-reward-image.sh <intensity> [task_title] [streak_count]
#
# Optional context:
#   REWARD_STATE_FILE=/path/to/state.json
#   REWARD_WORK_TYPE=focus|creative|social|independent
#   REWARD_ENERGY_LEVEL=low|medium|high
#
# Intensity levels: low, medium, high, epic
# Output: writes image to /tmp/reward-<timestamp>.png and prints the path

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$SCRIPT_DIR/.."

# --- Offline fallback rewards ---
# When image generation is unavailable (API outage, missing key, network error),
# suggest a real-life non-digital reward so the user still gets a celebration.
OFFLINE_REWARDS=(
    "Grab your favorite snack - you earned it!"
    "Treat yourself to a cupcake or some ice cream!"
    "Play your favorite video game for 30 minutes - guilt-free!"
    "Make yourself a fancy coffee or hot chocolate."
    "Take a walk outside and enjoy the fresh air for 15 minutes."
    "Put on your favorite song and have a mini dance party!"
    "Call or text a friend you haven't talked to in a while."
    "Watch an episode of your favorite show - you deserve a break!"
    "Grab a piece of chocolate or your favorite candy."
    "Do some stretches or a quick yoga session - treat your body!"
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

    echo "Image generation unavailable ($reason) - here's a real-life reward instead:" >&2
    echo "  [reward] $suggestion" >&2

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

for cmd in python3 curl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        suggest_offline_reward "$cmd not found"
    fi
done

INTENSITY="${1:-medium}"
TASK_TITLE_RAW="${2:-a task}"
STREAK="${3:-0}"
TIMESTAMP="$(date +%s)"
DATE_STR="$(date +%Y-%m-%d)"
TIME_STR="$(date +%H%M%S)"
REWARD_ID="${DATE_STR}_${TIME_STR}_${INTENSITY}_$RANDOM"
STATE_FILE="${REWARD_STATE_FILE:-$REPO_ROOT/state.json}"

case "$INTENSITY" in
    low|medium|high|epic)
        ;;
    *)
        echo "Unknown intensity: $INTENSITY" >&2
        exit 1
        ;;
esac

case "$STREAK" in
    ''|*[!0-9]*)
        STREAK=0
        ;;
esac

TASK_TITLE="$(printf '%s' "$TASK_TITLE_RAW" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//')"
[ -n "$TASK_TITLE" ] || TASK_TITLE="a task"

ARCHIVE_DIR="$REPO_ROOT/rewards"
MANIFEST_FILE="$ARCHIVE_DIR/manifest.log"
MANIFEST_JSONL="$ARCHIVE_DIR/manifest.jsonl"
FEEDBACK_JSONL="$ARCHIVE_DIR/feedback.jsonl"
mkdir -p "$ARCHIVE_DIR"

OUTPUT="/tmp/reward-${TIMESTAMP}.png"
ARCHIVE_FILE="${ARCHIVE_DIR}/${DATE_STR}_${TIME_STR}_${INTENSITY}.png"

build_reward_context() {
    INTENSITY="$INTENSITY" \
    TASK_TITLE="$TASK_TITLE" \
    STREAK="$STREAK" \
    REWARD_ID="$REWARD_ID" \
    STATE_FILE="$STATE_FILE" \
    FEEDBACK_FILE="$FEEDBACK_JSONL" \
    REWARD_WORK_TYPE="${REWARD_WORK_TYPE:-}" \
    REWARD_ENERGY_LEVEL="${REWARD_ENERGY_LEVEL:-}" \
    python3 <<'PY'
import json
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path


INTENSITY = os.environ["INTENSITY"]
TASK_TITLE = " ".join(os.environ["TASK_TITLE"].split())
STREAK = int(os.environ.get("STREAK", "0") or 0)
REWARD_ID = os.environ["REWARD_ID"]
STATE_FILE = Path(os.environ["STATE_FILE"])
FEEDBACK_FILE = Path(os.environ["FEEDBACK_FILE"])
WORK_TYPE = os.environ.get("REWARD_WORK_TYPE", "").strip().lower()
ENERGY_LEVEL = os.environ.get("REWARD_ENERGY_LEVEL", "").strip().lower()

THEMES = {
    "low": [
        {
            "id": "cozy_branch",
            "family": "nature",
            "scene": "a tiny bird landing on a branch while a small shower of sparkles drifts around it",
            "tags": ["nature", "animal", "soft", "warm"],
        },
        {
            "id": "twilight_firework",
            "family": "abstract",
            "scene": "a single elegant firework blooming in a dusk sky with soft pastel sparks",
            "tags": ["abstract", "light", "soft", "celebration"],
        },
        {
            "id": "sunbeam_cat",
            "family": "cozy",
            "scene": "a content cat stretching in a sunbeam while little celebratory glints hover nearby",
            "tags": ["animal", "cozy", "warm", "playful"],
        },
        {
            "id": "sprout_glow",
            "family": "growth",
            "scene": "a fresh sprout glowing gently as tiny lantern-like lights bloom around it",
            "tags": ["growth", "nature", "light", "soft"],
        },
        {
            "id": "paper_plane",
            "family": "motion",
            "scene": "a paper airplane arcing through peach sunrise clouds with subtle confetti trails",
            "tags": ["motion", "sky", "light", "playful"],
        },
    ],
    "medium": [
        {
            "id": "fox_meadow",
            "family": "animals",
            "scene": "a fox doing a victory dance in a meadow full of wildflowers",
            "tags": ["animal", "nature", "playful", "joy"],
        },
        {
            "id": "confetti_box",
            "family": "celebration",
            "scene": "colorful confetti exploding from a gift box with sparkles everywhere",
            "tags": ["abstract", "confetti", "colorful", "joy"],
        },
        {
            "id": "rocket_ring",
            "family": "space",
            "scene": "a rocket launching through a ring of stars with triumphant motion",
            "tags": ["space", "motion", "triumph", "bold"],
        },
        {
            "id": "otter_waterfall",
            "family": "animals",
            "scene": "an otter sliding down a rainbow waterfall with pure joy",
            "tags": ["animal", "water", "playful", "joy"],
        },
        {
            "id": "aurora_peak",
            "family": "nature",
            "scene": "a mountain peak at golden hour with aurora light pouring overhead",
            "tags": ["nature", "mountain", "majestic", "light"],
        },
    ],
    "high": [
        {
            "id": "phoenix_rise",
            "family": "mythic",
            "scene": "a magnificent phoenix rising from golden flames into a starlit sky",
            "tags": ["mythic", "fire", "majestic", "triumph"],
        },
        {
            "id": "aurora_dragon",
            "family": "mythic",
            "scene": "a dragon made of northern lights soaring over a glowing cityscape",
            "tags": ["mythic", "light", "majestic", "space"],
        },
        {
            "id": "astronaut_flag",
            "family": "space",
            "scene": "an astronaut planting a flag on a new planet with galaxies swirling behind them",
            "tags": ["space", "triumph", "bold", "exploration"],
        },
        {
            "id": "tree_of_light",
            "family": "growth",
            "scene": "a massive tree of light growing from the ground through clouds into space",
            "tags": ["growth", "light", "nature", "majestic"],
        },
        {
            "id": "whale_nebula",
            "family": "space",
            "scene": "a whale breaching through an ocean of stars and nebulae",
            "tags": ["space", "animal", "majestic", "wonder"],
        },
    ],
    "epic": [
        {
            "id": "galactic_crown",
            "family": "cosmic",
            "scene": "an entire galaxy forming the shape of a crown while supernovas bloom in celebration",
            "tags": ["space", "cosmic", "crown", "epic"],
        },
        {
            "id": "titan_prism",
            "family": "abstract",
            "scene": "a titan standing atop a mountain while reality fractures into prismatic light",
            "tags": ["abstract", "light", "epic", "majestic"],
        },
        {
            "id": "sun_moon_high_five",
            "family": "cosmic",
            "scene": "the sun and moon aligned in a cosmic high-five with rings of fire and ice",
            "tags": ["space", "cosmic", "playful", "epic"],
        },
        {
            "id": "galaxy_phoenix",
            "family": "mythic",
            "scene": "a colossal phoenix made of galaxies rising above a glowing planet",
            "tags": ["mythic", "space", "fire", "epic"],
        },
        {
            "id": "light_cathedral",
            "family": "abstract",
            "scene": "reality folding into a vast cathedral of light and color",
            "tags": ["abstract", "light", "epic", "wonder"],
        },
    ],
}

STYLE_OPTIONS = [
    {"name": "digital illustration", "tags": ["clean", "graphic", "versatile"]},
    {"name": "storybook watercolor", "tags": ["soft", "warm", "nature"]},
    {"name": "paper collage illustration", "tags": ["playful", "texture", "craft"]},
    {"name": "soft 3D render", "tags": ["bold", "light", "playful"]},
    {"name": "gouache poster art", "tags": ["graphic", "warm", "bold"]},
]

PALETTE_OPTIONS = [
    {"name": "sunrise citrus and confetti gold", "tags": ["warm", "bright", "celebration"]},
    {"name": "aurora jewel tones", "tags": ["cool", "space", "bold"]},
    {"name": "cozy pastel glow", "tags": ["soft", "warm", "cozy"]},
    {"name": "meadow greens and sky blues", "tags": ["nature", "fresh", "calm"]},
    {"name": "prismatic neon confetti", "tags": ["bold", "confetti", "playful"]},
]

TASK_PROFILES = [
    {
        "id": "communication",
        "patterns": [
            r"\b(call|phone|dial|email|text|message|reply|schedule|book|appointment|meeting)\b",
        ],
        "literal": "a glowing phone or note card bursting with confetti and relief",
        "metaphor": "a bright doorway opening with warm light and tiny celebratory sparks",
        "tags": ["communication", "admin", "relief"],
    },
    {
        "id": "writing",
        "patterns": [
            r"\b(write|draft|edit|revise|review|report|proposal|essay|notes?|read|study)\b",
        ],
        "literal": "pages and pens transforming into a lifted ribbon of stars",
        "metaphor": "scattered pages folding into one clear beam of light",
        "tags": ["writing", "paper", "focus"],
    },
    {
        "id": "technology",
        "patterns": [
            r"\b(code|debug|build|fix|ship|deploy|setup|install|configure|update|refactor|script)\b",
        ],
        "literal": "a friendly laptop radiating upgrade sparks and neat glowing circuitry",
        "metaphor": "gears of light clicking smoothly into place with an upward burst",
        "tags": ["technology", "build", "focus"],
    },
    {
        "id": "organization",
        "patterns": [
            r"\b(clean|tidy|organize|organise|declutter|laundry|dishes|sort)\b",
        ],
        "literal": "objects snapping into colorful order with satisfying sparkles",
        "metaphor": "chaos settling into calm symmetry under warm golden light",
        "tags": ["organization", "home", "order"],
    },
    {
        "id": "movement",
        "patterns": [
            r"\b(run|walk|workout|exercise|gym|stretch|bike)\b",
        ],
        "literal": "a bright path lighting up under energetic footsteps",
        "metaphor": "a trail of light unfurling forward with steady momentum",
        "tags": ["movement", "outdoors", "health"],
    },
    {
        "id": "food",
        "patterns": [
            r"\b(cook|meal|dinner|lunch|breakfast|grocery|groceries|shop)\b",
        ],
        "literal": "fresh ingredients and grocery shapes leaping upward in a burst of color",
        "metaphor": "a welcoming table glow building into a festive burst",
        "tags": ["food", "home", "care"],
    },
    {
        "id": "admin",
        "patterns": [
            r"\b(pay|bill|budget|tax|invoice|insurance|form|paperwork)\b",
        ],
        "literal": "a neat stack of forms resolving into golden confetti",
        "metaphor": "heavy blocks lifting into crisp floating ribbons of light",
        "tags": ["admin", "paper", "relief"],
    },
]

SENSITIVE_PATTERNS = [
    (r"\b(therapy|therapist|counseling|counselling|psychiatry|psychiatrist|trauma|grief|breakup|funeral)\b", "personal"),
    (r"\b(medical|doctor|hospital|clinic|prescription|medication|meds|diagnosis|lab|surgery|procedure|dentist|dental)\b", "medical"),
    (r"\b(tax|bank|credit|debt|loan|lawsuit|lawyer|court|insurance appeal|claim)\b", "financial_or_legal"),
]

GENERIC_TITLES = {
    "task",
    "tasks",
    "todo",
    "to do",
    "work",
    "stuff",
    "things",
    "errands",
    "chores",
    "misc",
    "miscellaneous",
}

WORK_TYPE_GUIDANCE = {
    "focus": "Favor a clean, structured composition with one strong focal point.",
    "creative": "Favor inventive motion and surprising details.",
    "social": "Favor warm, connected energy without depicting private people literally.",
    "independent": "Favor self-directed momentum and satisfying completion.",
}

ENERGY_GUIDANCE = {
    "low": "Keep the mood gentle, reassuring, and non-overwhelming.",
    "medium": "Keep the energy balanced and confident.",
    "high": "Use punchier motion and stronger contrast.",
}

HUMOR_GUIDANCE = {
    "subtle": "Keep the whimsy elegant and restrained.",
    "playful": "Add lighthearted celebratory charm.",
    "maximal": "Lean into exuberant, surprising celebratory details.",
}

HUMOR_GUIDANCE_KEYS = set(HUMOR_GUIDANCE)


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def listify(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def normalize_text_list(values):
    normalized = []
    for value in values:
        text = re.sub(r"\s+", " ", str(value).strip().lower())
        if text:
            normalized.append(text)
    return normalized


def term_variants(term: str):
    variants = {term}
    if term.endswith("s") and len(term) > 3:
        variants.add(term[:-1])
    else:
        variants.add(f"{term}s")
    return [variant for variant in variants if variant]


def load_reward_preferences(path: Path):
    if not path.is_file():
        return {
            "styles": [],
            "palettes": [],
            "subjects": [],
            "avoid": [],
            "humor_level": "playful",
        }

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "styles": [],
            "palettes": [],
            "subjects": [],
            "avoid": [],
            "humor_level": "playful",
        }

    reward_prefs = ((payload.get("user_preferences") or {}).get("rewards")) or {}
    if not isinstance(reward_prefs, dict):
        reward_prefs = {}

    humor = str(reward_prefs.get("humor_level") or "playful").strip().lower()
    if humor in {"low", "minimal"}:
        humor = "subtle"
    elif humor in {"high", "loud"}:
        humor = "maximal"
    elif humor not in HUMOR_GUIDANCE_KEYS:
        humor = "playful"

    return {
        "styles": normalize_text_list(listify(reward_prefs.get("preferred_styles") or reward_prefs.get("style"))),
        "palettes": normalize_text_list(listify(reward_prefs.get("preferred_palettes") or reward_prefs.get("palette"))),
        "subjects": normalize_text_list(listify(reward_prefs.get("favorite_subjects") or reward_prefs.get("subjects"))),
        "avoid": normalize_text_list(listify(reward_prefs.get("avoid"))),
        "humor_level": humor,
    }


def load_feedback(path: Path):
    feedback = {
        "theme_family": {},
        "style": {},
        "palette": {},
        "theme_tag": {},
    }

    if not path.is_file():
        return feedback

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return feedback

    recent_lines = [line for line in lines if line.strip()][-50:]
    for line in recent_lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        score = {
            "positive": 2.0,
            "neutral": 0.0,
            "negative": -2.0,
        }.get(str(entry.get("reaction", "")).lower())
        if score is None:
            continue

        family = slugify(str(entry.get("theme_family") or ""))
        if family:
            feedback["theme_family"][family] = feedback["theme_family"].get(family, 0.0) + score

        style = slugify(str(entry.get("style") or ""))
        if style:
            feedback["style"][style] = feedback["style"].get(style, 0.0) + score

        palette = slugify(str(entry.get("palette") or ""))
        if palette:
            feedback["palette"][palette] = feedback["palette"].get(palette, 0.0) + score

        for tag in entry.get("theme_tags", []):
            tag_key = slugify(str(tag))
            if tag_key:
                feedback["theme_tag"][tag_key] = feedback["theme_tag"].get(tag_key, 0.0) + score

    return feedback


def find_task_profile(title: str):
    lowered = title.lower()
    for profile in TASK_PROFILES:
        for pattern in profile["patterns"]:
            if re.search(pattern, lowered):
                return profile
    return {
        "id": "general",
        "literal": "a compact burst of progress and confetti radiating from a finished checkmark-shaped glow",
        "metaphor": "a spark of momentum widening into a bright, satisfying celebration",
        "tags": ["general", "progress"],
    }


def detect_sensitive_reason(title: str):
    lowered = title.lower()
    for pattern, reason in SENSITIVE_PATTERNS:
        if re.search(pattern, lowered):
            return reason
    return ""


def is_vague_title(title: str) -> bool:
    lowered = title.lower().strip()
    if lowered in GENERIC_TITLES:
        return True
    words = re.findall(r"[a-z0-9']+", lowered)
    if not words:
        return True
    if len(words) <= 2 and all(word in GENERIC_TITLES for word in words):
        return True
    return False


def pick_with_weights(options, scorer):
    weights = []
    for option in options:
        weight = scorer(option)
        weights.append(max(weight, 0.1))
    return random.choices(options, weights=weights, k=1)[0]


def option_text(option):
    parts = [
        option.get("id", ""),
        option.get("family", ""),
        option.get("name", ""),
        option.get("scene", ""),
        " ".join(option.get("tags", [])),
    ]
    return " ".join(parts).lower()


def preference_score(option, preferred_terms, weight):
    joined = option_text(option)
    score = 0.0
    for term in preferred_terms:
        for variant in term_variants(term):
            if variant and variant in joined:
                score += weight
                break
    return score


def avoidance_penalty(option, avoid_terms, weight):
    joined = option_text(option)
    penalty = 0.0
    for term in avoid_terms:
        for variant in term_variants(term):
            if variant and variant in joined:
                penalty += weight
                break
    return penalty


def build_streak_detail(streak: int) -> str:
    if streak >= 5:
        return "Add a trail of five glowing orbs to signal a hot streak."
    if streak >= 3:
        return "Add three small orbiting stars to hint at momentum."
    return ""


def build_task_mode(task_title: str, sensitive_reason: str):
    if sensitive_reason:
        return "metaphorical"
    if is_vague_title(task_title):
        return "abstract"
    return "literal"


def build_task_motif(profile, task_mode: str):
    if task_mode == "literal":
        return profile["literal"]
    if task_mode == "metaphorical":
        return profile["metaphor"]
    return "a clean burst of forward motion turning into a satisfying celebration"


def theme_score(option, prefs, feedback, task_mode):
    score = 1.0
    score += preference_score(option, prefs["subjects"], 1.2)
    score -= avoidance_penalty(option, prefs["avoid"], 1.5)
    score += feedback["theme_family"].get(slugify(option.get("family", "")), 0.0) * 0.35
    for tag in option.get("tags", []):
        score += feedback["theme_tag"].get(slugify(tag), 0.0) * 0.12
    if task_mode == "metaphorical":
        if "playful" in option.get("tags", []):
            score -= 0.25
        if any(tag in option.get("tags", []) for tag in ("abstract", "light", "growth", "majestic")):
            score += 0.45
    return score


def style_score(option, prefs, feedback, task_mode):
    score = 1.0
    score += preference_score(option, prefs["styles"], 1.3)
    score -= avoidance_penalty(option, prefs["avoid"], 1.2)
    score += feedback["style"].get(slugify(option["name"]), 0.0) * 0.4
    if task_mode == "metaphorical" and "playful" in option.get("tags", []):
        score -= 0.15
    return score


def palette_score(option, prefs, feedback):
    score = 1.0
    score += preference_score(option, prefs["palettes"], 1.3)
    score += preference_score(option, prefs["subjects"], 0.4)
    score -= avoidance_penalty(option, prefs["avoid"], 1.1)
    score += feedback["palette"].get(slugify(option["name"]), 0.0) * 0.4
    return score


reward_preferences = load_reward_preferences(STATE_FILE)
feedback = load_feedback(FEEDBACK_FILE)
task_profile = find_task_profile(TASK_TITLE)
sensitive_reason = detect_sensitive_reason(TASK_TITLE)
task_mode = build_task_mode(TASK_TITLE, sensitive_reason)
task_motif = build_task_motif(task_profile, task_mode)

theme = pick_with_weights(
    THEMES[INTENSITY],
    lambda option: theme_score(option, reward_preferences, feedback, task_mode),
)
style = pick_with_weights(
    STYLE_OPTIONS,
    lambda option: style_score(option, reward_preferences, feedback, task_mode),
)
palette = pick_with_weights(
    PALETTE_OPTIONS,
    lambda option: palette_score(option, reward_preferences, feedback),
)

humor_level = reward_preferences["humor_level"]
if sensitive_reason and humor_level == "maximal":
    humor_level = "playful"

prompt_parts = [
    "Create a square celebratory reward image for a completed task.",
    f"Core scene: {theme['scene']}.",
    f"Blend in task relevance with {task_motif}.",
]

if sensitive_reason:
    prompt_parts.append(
        "Keep the celebration metaphorical and non-specific. Do not depict private medical, legal, financial, or relationship details literally."
    )
elif task_mode == "abstract":
    prompt_parts.append(
        "The task title is vague, so favor a universally satisfying sense of progress over literal objects."
    )

prompt_parts.extend(
    [
        f"Style: {style['name']}.",
        f"Palette: {palette['name']}.",
        WORK_TYPE_GUIDANCE.get(WORK_TYPE, ""),
        ENERGY_GUIDANCE.get(ENERGY_LEVEL, ""),
        HUMOR_GUIDANCE[humor_level],
        build_streak_detail(STREAK),
        "Keep the composition uplifting, polished, and easy to read at a glance.",
        "No text, no letters, no numbers, no UI, no watermarks.",
    ]
)

prompt = " ".join(part for part in prompt_parts if part)

context = {
    "reward_id": REWARD_ID,
    "prompt_version": 2,
    "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "intensity": INTENSITY,
    "task_title": TASK_TITLE,
    "streak": STREAK,
    "task_mode": task_mode,
    "task_profile": task_profile["id"],
    "task_motif": task_motif,
    "task_tags": list(dict.fromkeys(task_profile["tags"] + ([WORK_TYPE] if WORK_TYPE else []) + ([ENERGY_LEVEL] if ENERGY_LEVEL else []))),
    "sensitive_reason": sensitive_reason or None,
    "theme_id": theme["id"],
    "theme_family": theme["family"],
    "theme_tags": theme["tags"],
    "style": style["name"],
    "palette": palette["name"],
    "humor_level": humor_level,
    "work_type": WORK_TYPE or None,
    "energy_level": ENERGY_LEVEL or None,
    "feedback_active": FEEDBACK_FILE.is_file(),
    "preferences_active": STATE_FILE.is_file() and bool(
        reward_preferences["styles"]
        or reward_preferences["palettes"]
        or reward_preferences["subjects"]
        or reward_preferences["avoid"]
        or reward_preferences["humor_level"] != "playful"
    ),
    "prompt": prompt,
}

print(json.dumps(context, ensure_ascii=True))
PY
}

PROMPT_CONTEXT_JSON="$(build_reward_context)"
PROMPT="$(printf '%s' "$PROMPT_CONTEXT_JSON" | python3 -c 'import json, sys; print(json.load(sys.stdin)["prompt"])')"

if [ "$INTENSITY" = "epic" ]; then
    QUALITY="high"
else
    QUALITY="auto"
fi

set +e
RESPONSE="$(
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
)"
CURL_EXIT=$?
set -e

if [ "$CURL_EXIT" -ne 0 ] || [ -z "$RESPONSE" ]; then
    suggest_offline_reward "network error or API unreachable"
fi

ERROR="$(echo "$RESPONSE" | python3 -c "
import json, sys
data = json.load(sys.stdin)
if 'error' in data:
    print(data['error']['message'])
" 2>/dev/null || true)"

if [ -n "$ERROR" ]; then
    suggest_offline_reward "API error: $ERROR"
fi

set +e
OUTPUT_PATH="$OUTPUT" python3 -c "
import base64
import json
import os
import sys

data = json.load(sys.stdin)
result = data['data'][0]
output_path = os.environ['OUTPUT_PATH']

if 'b64_json' in result:
    image_data = base64.b64decode(result['b64_json'])
    with open(output_path, 'wb') as handle:
        handle.write(image_data)
elif 'url' in result:
    import urllib.request
    urllib.request.urlretrieve(result['url'], output_path)
" <<< "$RESPONSE"
EXTRACT_EXIT=$?
set -e

if [ "$EXTRACT_EXIT" -ne 0 ] || [ ! -s "$OUTPUT" ]; then
    suggest_offline_reward "failed to decode image from API response"
fi

cp "$OUTPUT" "$ARCHIVE_FILE"

printf '%s\t%s\t%s\t%s\n' "$TIMESTAMP" "$INTENSITY" "$TASK_TITLE" "$ARCHIVE_FILE" >> "$MANIFEST_FILE"

MANIFEST_RECORD="$(
    CONTEXT_JSON="$PROMPT_CONTEXT_JSON" \
    TIMESTAMP="$TIMESTAMP" \
    ARCHIVE_FILE="$ARCHIVE_FILE" \
    OUTPUT_FILE="$OUTPUT" \
    python3 <<'PY'
import json
import os

record = json.loads(os.environ["CONTEXT_JSON"])
record.update(
    {
        "timestamp": int(os.environ["TIMESTAMP"]),
        "archive_file": os.environ["ARCHIVE_FILE"],
        "output_file": os.environ["OUTPUT_FILE"],
    }
)
print(json.dumps(record, ensure_ascii=True))
PY
)"
printf '%s\n' "$MANIFEST_RECORD" >> "$MANIFEST_JSONL"

echo "$OUTPUT"
