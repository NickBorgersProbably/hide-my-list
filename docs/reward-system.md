---
layout: default
title: Reward System
---

# Reward System

## Overview

Reward system = core hide-my-list component. Dopamine-inducing positive reinforcement on task completion/progress. Multiple reward channels — system-generated + interpersonal — create motivation loop.

## Reward Philosophy

```mermaid
mindmap
  root((Dopamine<br/>Delivery))
    System Generated
      Visual Celebration
        Emoji explosions
        AI-generated images
        AI-generated videos
      Audio Celebration
        Favorite songs
        Victory sounds
        Home audio integration
    Interpersonal
      Significant Other
        Completion texts
        Progress updates
        Shared victories
      Self-Reward
        Outing suggestions
        Interest-aligned treats
        Break recommendations
```

Principle: **completing tasks should feel genuinely rewarding**. Achieved via:

1. **Immediate gratification** - Instant visual/audio feedback
2. **Social reinforcement** - Loved ones acknowledge achievements
3. **Anticipatory pleasure** - Suggestions for enjoyable activities

### Shame-Safe Reward Principles

> **Shame Prevention:** Reward system must never create implicit comparison between "good" sessions (many completions) and "bad" sessions (few or none). Rewards celebrate what happened, never highlight what didn't.

- **Celebrate effort, not just results** — "You showed up and tried today. That counts."
- **Never reference streak breaks negatively** — streak ends: don't mention. Just start fresh.
- **Partial progress is real progress** — sub-task completion deserves acknowledgment
- **Safe exits get warmth, not silence** — "See you next time" beats no response
- **No guilt-inducing comparisons** — never "You did 3 tasks yesterday but only 1 today"

---

## Reward Architecture

```mermaid
flowchart TB
    subgraph Trigger["Reward Triggers"]
        Initiation[Task Started]
        FirstStep[First Step Done]
        Resume[Resumed After Break]
        Complete[Task Completed]
        Streak[Streak Achieved]
        Milestone[Milestone Reached]
    end

    subgraph Engine["Reward Engine"]
        Select[Reward Selector]
        Scale[Intensity Scaler]
        Deliver[Multi-channel Delivery]
    end

    subgraph SystemRewards["System-Generated Rewards"]
        Emoji[Emoji Celebration]
        Image[AI-Generated Image]
        Video[AI Video<br/>ffmpeg]
        Music[Music Playback<br/>Home Audio]
    end

    subgraph InterpersonalRewards["Interpersonal Rewards"]
        TextSO[Text Significant Other]
        Outing[Suggest Outing]
    end

    Initiation --> Select
    FirstStep --> Select
    Resume --> Select
    Complete --> Select
    Streak --> Select
    Milestone --> Select

    Select --> Scale
    Scale --> Deliver

    Deliver --> Emoji
    Deliver --> Image
    Deliver --> Video
    Deliver --> Music
    Deliver --> TextSO
    Deliver --> Outing
```

> **Note:** All initiation-phase triggers (Task Started, First Step Done, Resumed After Break)
> fire as reward events within the **Active** conversation state — they are not separate
> conversation states. See the conversation state diagram in `docs/ai-prompts/shared.md`.

---

## System-Generated Rewards

### Emoji Celebrations

Emoji-loaded congratulations messages scaling with achievement significance.

```mermaid
flowchart LR
    subgraph Intensity["Celebration Intensity"]
        Low["Single Task<br/>Completed"]
        Medium["Difficult Task<br/>or 3-task Streak"]
        High["Major Milestone<br/>or 5+ Streak"]
        Epic["Parent Task Complete<br/>or Day Clear"]
    end

    subgraph Output["Emoji Output"]
        LowEmoji["Nice work! ✨"]
        MedEmoji["Crushing it! 🎉✨💪"]
        HighEmoji["UNSTOPPABLE! 🔥🎉✨💪🚀"]
        EpicEmoji["LEGENDARY! 🏆👑🔥🎉✨💪🚀⭐"]
    end

    Low --> LowEmoji
    Medium --> MedEmoji
    High --> HighEmoji
    Epic --> EpicEmoji
```

#### Initiation Reward Templates (Issue #7)

> **Design principle:** Starting harder than finishing for ADHD brains. Initiation rewards acknowledge this truth. Feel like genuine encouragement from someone who understands, not participation trophies. Keep brief — user about to start working.

```mermaid
flowchart TD
    subgraph Triggers["Initiation Trigger Points"]
        Accept[User accepts task] --> StartReward[Initiation Reward]
        FirstStep[First sub-step completed] --> ProgressReward[First-Step Reward]
        Return[User returns to paused task] --> ResumeReward[Resume Reward]
    end

    subgraph Scoring["Unified Scoring (see Reward Scaling Algorithm)"]
        Calc["Score calculated using<br/>base_score + streak_bonus − diminishing"]
        Cap["Capped by initiation_ceiling<br/>to keep lighter than completion"]
    end

    subgraph Intensity["Maps to Unified Intensity Levels"]
        Lightest["Score 0-10 → Lightest<br/>Brief acknowledgment"]
        Low["Score 11-25 → Low<br/>Momentum confirmation"]
        Medium["Score 26-50 → Medium<br/>(max for initiation triggers)"]
    end

    StartReward --> Calc
    ProgressReward --> Calc
    ResumeReward --> Calc
    Calc --> Cap
    Cap --> Intensity
```

Initiation rewards use **same scoring algorithm** as completion rewards
(see [Reward Scaling Algorithm](#reward-scaling-algorithm)), with two
initiation-specific adjustments:

1. **`initiation_base_weight`** — multiplier (default `0.4`) on base score, keeping initiation rewards inherently lighter.
2. **`initiation_ceiling`** — intensity cap (default `Medium / 50`) preventing initiation rewards from reaching `High` or `Epic`, preserving those tiers for completion.

| Trigger | Base-Weight | Ceiling | Example Messages |
|---------|-------------|---------|------------------|
| Task accepted (starting) | 0.3 | Lightest (10) | "You're in. That's the hardest part.", "Starting — nice.", "Let's go." |
| First sub-step done | 0.4 | Low (25) | "First step done — you're in motion now.", "One down. Momentum's real." |
| Resumed after break | 0.5 | Medium (50) | "Back at it — picking up where you left off is a skill.", "Welcome back. Ready to keep going?" |
| Started 3+ tasks today | 0.4 | Low (25) | "Third start today — your initiation muscle is getting stronger." |

**Important design constraints:**
- Initiation rewards must be **briefer and lighter** than completion rewards
- Never celebrate starting so much it diminishes completion celebration
- Tone is **acknowledgment of difficulty**, not generic cheerleading
- "You started" validates that starting is genuinely hard — don't trivialize it
- First-time users always get initiation reward; returning users: vary frequency to avoid habituation (see Issue #12 for novelty)

#### Completion Celebration Message Templates

| Trigger | Intensity | Example Messages |
|---------|-----------|------------------|
| Single task | Low | "Nice work! ✨", "Done! 💫", "Got it! ✅" |
| Quick task (< 15 min) | Low | "Speed demon! ⚡", "Quick win! 🎯" |
| Focus task complete | Medium | "Deep work done! 🧠✨", "Focus mode: crushed! 💪🎯" |
| 3-task streak | Medium | "Hat trick! 🎩✨🎉", "Three down! 🔥💪" |
| 5-task streak | High | "On fire! 🔥🔥🔥✨💪", "Unstoppable! 🚀🎉💪" |
| Difficult task | High | "Beast mode! 💪🔥🎉", "Conquered! ⚔️✨🏆" |
| Parent task (all subs done) | Epic | "MAJOR WIN! 🏆👑🎉✨🔥", "PROJECT COMPLETE! 🚀⭐💪🎊" |
| All tasks cleared | Epic | "INBOX ZERO! 🏆👑✨🎉🔥💪🚀", "LEGENDARY DAY! 👑⭐🏆🎊" |

---

### AI-Generated Celebration Images

Every completion gets **unique, AI-generated celebration image** via OpenAI's `gpt-image-1` model. Novelty ADHD brains crave — no two celebrations identical, prevents habituation, maintains dopamine response.

#### Why AI-Generated Images

- **Novelty**: ADHD brains habituate to repeated stimuli. Every AI image unique, no predictability.
- **Dopamine**: Novel visual stimuli trigger stronger dopamine than familiar ones.
- **Personalization**: Prompts incorporate user context, streaks, preferences.
- **Scalability**: No static image library to curate/maintain.

```mermaid
flowchart TD
    subgraph Trigger["Completion Trigger"]
        Complete[Task Completed]
        Intensity[Calculate Intensity]
    end

    subgraph Generation["Image Generation"]
        Theme[Select Random Theme Pool]
        Prompt[Build Prompt]
        Style[Apply Intensity Styling]
        Generate[OpenAI gpt-image-1 API]
    end

    subgraph Delivery["Image Delivery"]
        Chat[Embed in Chat Message]
        Signal[Send via Signal/Telegram]
    end

    Complete --> Intensity
    Intensity --> Theme
    Theme --> Prompt
    Prompt --> Style
    Style --> Generate
    Generate --> Delivery
```

#### Image Generation Script

`scripts/generate-reward-image.sh` handles all image generation:

```bash
# Generate a reward image
./scripts/generate-reward-image.sh <intensity> [task_title] [streak_count]

# Examples:
./scripts/generate-reward-image.sh low "Call dentist" 0
./scripts/generate-reward-image.sh medium "Review proposal" 2
./scripts/generate-reward-image.sh epic "Complete Q4 report" 5
```

Output: writes PNG to `/tmp/reward-<timestamp>.png` and prints the path.

#### Theme Pools by Intensity

Each intensity level has 5+ thematic prompts. Random theme selected each time for variety.

| Intensity | Theme Style | Examples |
|-----------|-------------|---------|
| Low | Gentle, warm, cozy | Cheerful bird with sparkle, paper airplane in clouds, happy cat in sunbeam |
| Medium | Enthusiastic, joyful | Fox dancing in wildflowers, confetti explosion, otter on rainbow waterfall |
| High | Majestic, powerful | Phoenix rising from golden flames, astronaut planting flag, whale in starfield |
| Epic | Cosmic, transcendent | Galaxy forming a crown, reality folding into light cathedral, cosmic phoenix |

#### Streak Enhancements

Streak count modifies generated image:

| Streak | Visual Enhancement |
|--------|--------------------|
| 0-2 | Base theme only |
| 3-4 | Three orbiting stars added |
| 5+ | Trail of five glowing orbs added |

#### Novelty Mechanics (Issue #12)

Image generation system inherently addresses novelty:

1. **Random theme selection** — each intensity has 5+ themes, randomly chosen
2. **AI variation** — same prompt produces different images each time
3. **Streak-responsive** — visual elements change as streaks grow
4. **Expandable pools** — new themes added to script without code changes

Future enhancements:
- Seasonal/holiday theme injection
- User preference learning (track which themes get positive reactions)
- Milestone surprise themes (hidden achievements at 10th, 50th, 100th task)

#### Graceful Degradation — Offline Fallback Rewards

If image generation unavailable (API outage, missing key, network error, malformed response), script **does not fail silently**. Suggests fun non-digital real-life reward from pool of 12:

- Favorite snack, cupcake, ice cream, chocolate
- 30 minutes of a favorite video game
- Fancy coffee or hot chocolate
- A walk outside, stretches, or yoga
- Mini dance party, calling a friend, watching a show
- Ordering favorite takeout

Fallback writes suggestion to `.txt` file (instead of `.png`) and exits successfully — reward pipeline always delivers something. Prevents "expected reward didn't arrive" anti-pattern from Hallowell-Ratey's ADHD framework.

#### Environment Variables

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | OpenAI API authentication for image generation |

#### Image Archive & Collection

Every generated reward image auto-archived to `rewards/` with metadata:

- **File naming**: `YYYY-MM-DD_HHMMSS_<intensity>.png`
- **Manifest log**: `rewards/manifest.log` tracks timestamp, intensity, task title, file path
- **Persistent**: Images survive across sessions — celebration history preserved

#### Weekly Recap Video

`scripts/generate-weekly-recap.sh` compiles all reward images from past week into card-flip transition video:

```bash
# Generate recap of past 7 days (default)
./scripts/generate-weekly-recap.sh

# Custom range
./scripts/generate-weekly-recap.sh 14  # past 2 weeks
```

Features:
- **Card-flip transitions** between images (fadegrays, circlecrop, radial, etc.)
- **Variety in transitions** — each cut uses different style
- **Fade-out ending** for polished finish
- **Output**: `rewards/weekly-recap-YYYY-MM-DD.mp4`

Recap = tangible accomplishment record — scrolling a week of unique celebration images is itself a reward.

```mermaid
flowchart LR
    subgraph Archive["Image Archive"]
        Mon["Mon: 2 images"]
        Tue["Tue: 3 images"]
        Wed["Wed: 1 image"]
        Thu["Thu: 4 images"]
        Fri["Fri: 2 images"]
    end

    subgraph Recap["Weekly Recap"]
        Video["Card-flip video<br/>12 images, ~40 seconds"]
    end

    Archive --> Recap
    Recap --> Deliver["Send via Signal/Telegram"]
```

#### Technical Details

| Setting | Value |
|---------|-------|
| Model | `gpt-image-1` |
| Size | 1024x1024 |
| Quality | `auto` (low-high), `high` (epic) |
| Output format | PNG (images), MP4 (recap video) |
| Typical generation time | 10-20 seconds |
| Archive location | `rewards/` |
| Video codec | H.264 (libx264) |
| Display per image | 2.5 seconds |
| Transition duration | 0.8 seconds |

---

### Music Playback (Home Audio Integration)

Home automation plays celebratory music on task completion.

```mermaid
flowchart TB
    subgraph HomeAudio["Home Audio Systems"]
        Sonos[Sonos]
        HomePod[Apple HomePod]
        Echo[Amazon Echo]
        GoogleHome[Google Home]
        Custom[Custom Systems<br/>via API/MQTT]
    end

    subgraph Integration["Integration Layer"]
        API[Home Automation API]
        MQTT[MQTT Bridge]
        HomeAssistant[Home Assistant]
    end

    subgraph Playback["Music Selection"]
        Favorites[User Favorites Playlist]
        Victory[Victory Songs Playlist]
        Mood[Mood-matched Music]
    end

    Complete([Task Completed]) --> Integration
    Integration --> HomeAudio
    Playback --> Integration
```

#### Music Playback Configuration

```mermaid
flowchart LR
    subgraph Config["User Configuration"]
        Enable["Enable/Disable"]
        Volume["Volume Level"]
        Duration["Play Duration<br/>15s / 30s / Full song"]
        Times["Active Hours<br/>Don't wake the baby"]
        Rooms["Target Rooms"]
    end

    subgraph Playlists["Playlist Sources"]
        Spotify[Spotify Playlist ID]
        Apple[Apple Music Playlist]
        Local[Local Music Library]
    end

    subgraph Rules["Playback Rules"]
        TimeCheck["Check active hours"]
        LocationCheck["Check presence"]
        FrequencyLimit["Rate limit<br/>Max 3/hour"]
    end

    Config --> Rules
    Playlists --> Rules
    Rules --> Play[Trigger Playback]
```

#### Example Music Triggers

| Achievement | Music Selection | Duration |
|-------------|-----------------|----------|
| Quick task | Random from "Victory Jingles" | 15 seconds |
| Focus task | Random from "Triumphant" | 30 seconds |
| Major milestone | User's favorite song | Full song |
| All tasks cleared | "We Are The Champions" | Full song |

#### Home Automation Integration Points

| System | Integration Method | Notes |
|--------|-------------------|-------|
| Sonos | Sonos API | Direct HTTP calls |
| Apple HomePod | HomeKit/Shortcuts | Via Shortcuts automation |
| Amazon Echo | Alexa Skills | Custom skill or routines |
| Google Home | Google Home API | Cast-enabled playback |
| Home Assistant | REST API | Universal bridge for any system |
| Custom | MQTT | Publish to configured topic |

---

## Interpersonal Rewards

### Text Significant Other

Auto-notify loved one on task completion — external positive reinforcement + social accountability.

```mermaid
flowchart TD
    subgraph Trigger["Completion Trigger"]
        Task["Task Completed"]
        Streak["Streak Achieved"]
        Parent["Project Finished"]
    end

    subgraph Filter["Notification Filter"]
        Frequency["Frequency Limit<br/>Max N per day"]
        Significance["Significance Threshold"]
        OptIn["Task Opt-in Check"]
    end

    subgraph Compose["Message Composition"]
        Template["Select Template"]
        Personalize["Add Task Context"]
        Tone["Match Relationship Tone"]
    end

    subgraph Deliver["Delivery"]
        SMS[SMS via Twilio]
        iMessage[iMessage via Shortcuts]
        WhatsApp[WhatsApp API]
        Telegram[Telegram Bot]
    end

    Trigger --> Filter
    Filter --> Compose
    Compose --> Deliver
```

#### Notification Configuration

| Setting | Options | Default |
|---------|---------|---------|
| recipient | Phone number or contact ID | Required |
| delivery_method | sms, imessage, whatsapp, telegram | sms |
| frequency_limit | 1-10 per day | 3 |
| min_significance | low, medium, high, epic | medium |
| active_hours | Time range | 9am-9pm |
| task_opt_in | all, tagged, manual | tagged |

#### Message Templates

```mermaid
flowchart LR
    subgraph Style["Message Styles"]
        Casual["Casual<br/>'Hey! [Name] just crushed [task]!'"]
        Supportive["Supportive<br/>'[Name] finished [task]! Maybe tell them nice work?'"]
        Celebratory["Celebratory<br/>'🎉 [Name] completed [task]! Celebration time!'"]
        Informative["Informative<br/>'FYI: [Name] completed [task]'"]
    end
```

| Trigger | Example Message |
|---------|-----------------|
| Single task | "Hey! [Name] just finished '[task]' - maybe give them a high five later? 🙌" |
| Streak (3+) | "[Name] is on a roll - [N] tasks done today! 🔥" |
| Difficult task | "[Name] just conquered a big one: '[task]'. They might need a hug! 💪" |
| Parent complete | "BIG NEWS: [Name] finished the entire '[project]'! Celebration dinner? 🎉" |
| All cleared | "[Name] cleared their ENTIRE task list! This calls for ice cream 🍦" |

#### Privacy & Consent

```mermaid
flowchart TD
    subgraph Consent["Consent Model"]
        UserConsent["User enables feature"]
        RecipientConsent["Recipient agrees to receive"]
        TaskTagging["User tags shareable tasks"]
    end

    subgraph Privacy["Privacy Controls"]
        Anonymize["Option to anonymize task names"]
        Categories["Share category only, not details"]
        Veto["User can veto before sending"]
    end

    Consent --> Active[Feature Active]
    Privacy --> Active
```

---

### Outing Suggestions

After completing tasks (especially difficult), suggest fun activities aligned with user interests — creates anticipation + self-reward.

```mermaid
flowchart TD
    subgraph Triggers["Suggestion Triggers"]
        MajorComplete["Major task completed"]
        DayClear["Day's tasks cleared"]
        LongStreak["Long streak achieved"]
        FridayComplete["Friday completions"]
    end

    subgraph Analysis["Context Analysis"]
        Time["Time of day"]
        Weather["Weather check"]
        Energy["User energy level"]
        Interests["User interests"]
        Location["User location"]
    end

    subgraph Suggestions["Outing Categories"]
        Food["Food & Drink<br/>Favorite restaurant, coffee shop"]
        Active["Active<br/>Hiking, gym, sports"]
        Social["Social<br/>Call friend, game night"]
        Relaxation["Relaxation<br/>Movie, spa, reading"]
        Adventure["Adventure<br/>New experience, exploration"]
    end

    Triggers --> Analysis
    Analysis --> Suggestions
    Suggestions --> Present[Present Suggestion]
```

#### User Interest Configuration

```mermaid
flowchart LR
    subgraph Interests["Interest Categories"]
        I1["Food preferences<br/>Cuisines, dietary"]
        I2["Activity level<br/>Low, medium, high"]
        I3["Social preference<br/>Solo, partner, group"]
        I4["Hobbies<br/>Sports, arts, games"]
        I5["Favorite spots<br/>Saved locations"]
    end

    subgraph Matching["Match Algorithm"]
        TimeMatch["Time-appropriate"]
        EnergyMatch["Energy-appropriate"]
        WeatherMatch["Weather-appropriate"]
        BudgetMatch["Budget-conscious"]
    end

    Interests --> Matching
    Matching --> Suggestion[Personalized Suggestion]
```

#### Suggestion Templates

| Context | Example Suggestions |
|---------|---------------------|
| After focus work (tired) | "You've earned a break! How about grabbing a coffee from [favorite_cafe]? ☕" |
| After physical task | "Nice work! Maybe reward yourself with [favorite_food] from [restaurant]? 🍕" |
| Friday afternoon | "Weekend's calling! Movie night with [partner] at [theater]? 🎬" |
| All tasks cleared | "EVERYTHING DONE! Time for an adventure - what about [saved_activity]? 🎉" |
| Long streak | "5 tasks in a row! You deserve [favorite_treat] 🏆" |
| Morning completion | "Great start! Save room for [lunch_spot] later? 🌮" |

#### External Integrations

| Service | Use Case |
|---------|----------|
| Google Maps | Location search, directions |
| Yelp API | Restaurant recommendations |
| Weather API | Weather-appropriate suggestions |
| Calendar | Check availability |
| Partner's calendar | Coordinate joint activities |

---

## Reward Scaling Algorithm

```mermaid
flowchart TD
    subgraph Input["Reward Inputs"]
        TaskDifficulty["Task Difficulty<br/>time + energy"]
        StreakCount["Current Streak"]
        TriggerType["Trigger Type<br/>initiation | completion"]
        TimeOfDay["Time of Day"]
        RecentRewards["Recent Reward History"]
        UserPrefs["User Preferences"]
    end

    subgraph Calculate["Intensity Calculation"]
        Base["Base Score<br/>from task difficulty"]
        Weight["Apply initiation_base_weight<br/>(1.0 for completion)"]
        Multiplier["Streak Bonus<br/>streak_count × 5"]
        Diminishing["Diminishing Returns<br/>reduce if many recent rewards"]
        Cap["Apply initiation_ceiling<br/>(100 for completion)"]
    end

    subgraph Output["Reward Selection"]
        Intensity["Intensity Level<br/>lightest|low|medium|high|epic"]
        Channels["Active Channels"]
        Content["Specific Content"]
    end

    Input --> Calculate
    Calculate --> Output
```

### Intensity Levels

| Level | Score Range | Emoji Count | AI Image | Music | Text SO | Outing | Used For |
|-------|-------------|-------------|----------|-------|---------|--------|----------|
| Lightest | 0-10 | 0 | No | No | No | No | Initiation only |
| Low | 11-25 | 1-2 | Gentle theme | No | No | No | Initiation + Completion |
| Medium | 26-50 | 2-4 | Enthusiastic theme | Maybe | Maybe | No | Initiation (max) + Completion |
| High | 51-75 | 4-6 | Majestic theme | Yes | Yes | Maybe | Completion only |
| Epic | 76-100 | 6+ | Cosmic theme (high quality) | Yes | Yes | Yes | Completion only |

### Score Calculation

Same formula for **both** initiation and completion rewards. Initiation triggers apply weight + ceiling to keep lighter.

```
# --- Shared base calculation (initiation + completion) ---
base_score = (time_estimate / 15) * 10 + (energy_level * 10)
streak_bonus = streak_count * 5
milestone_bonus = is_parent_complete ? 25 : 0
milestone_bonus += is_all_cleared ? 50 : 0

raw_score = base_score + streak_bonus + milestone_bonus
diminishing = max(0, (rewards_in_last_hour - 2) * 10)

# --- Completion rewards ---
completion_score = min(100, max(0, raw_score - diminishing))

# --- Initiation rewards ---
# initiation_base_weight: per-trigger multiplier (see table above)
#   task_accepted = 0.3, first_step = 0.4, resumed = 0.5, multi_start = 0.4
# initiation_ceiling: per-trigger max score
#   task_accepted = 10, first_step = 25, resumed = 50, multi_start = 25
weighted_score = (base_score * initiation_base_weight) + streak_bonus
initiation_score = min(initiation_ceiling, max(0, weighted_score - diminishing))
```

**Why two adjustments?**
- `initiation_base_weight` scales down task-difficulty component — user hasn't done work yet, only started.
- `initiation_ceiling` guarantees no initiation reward ever reaches `High` or `Epic`, keeping those tiers exclusively for completion. Starting never feels more rewarding than finishing.
- `streak_bonus` kept at full value for initiation — building a *starting* streak is genuinely hard for ADHD, deserves recognition.

---

## Configuration Schema

### User Preferences (stored in Notion or local config)

```mermaid
erDiagram
    USER_REWARD_PREFS {
        boolean emoji_enabled "Default: true"
        boolean image_enabled "Default: true"
        boolean music_enabled "Default: false"
        boolean video_enabled "Default: false"
        boolean text_so_enabled "Default: false"
        boolean outing_enabled "Default: true"
    }

    MUSIC_CONFIG {
        string home_system "sonos|homepod|echo|google|homeassistant"
        string playlist_id "Spotify/Apple Music ID"
        string[] target_rooms "Living Room, Office"
        int volume_level "0-100"
        string active_hours "09:00-21:00"
        int max_per_hour "3"
    }

    TEXT_SO_CONFIG {
        string recipient_phone "+1234567890"
        string delivery_method "sms|imessage|whatsapp|telegram"
        int max_per_day "3"
        string min_significance "medium"
        string active_hours "09:00-21:00"
        string message_style "casual|supportive|celebratory"
        boolean anonymize_tasks "false"
    }

    OUTING_CONFIG {
        string[] food_preferences "Italian, Mexican, Coffee"
        string activity_level "medium"
        string social_preference "partner"
        string[] favorite_spots "Cafe Luna, Central Park"
        string[] hobbies "hiking, movies, board games"
    }
```

---

## Integration with Existing Flows

### Completion Flow Enhancement

```mermaid
sequenceDiagram
    participant U as User
    participant AI as AI Assistant
    participant R as Reward Engine
    participant HA as Home Audio
    participant SMS as SMS Service
    participant N as Notion

    U->>AI: "Done!"
    AI->>N: Update task status → completed
    AI->>R: Trigger reward evaluation

    R->>R: Calculate intensity score
    R->>R: Select reward channels

    par Parallel Reward Delivery
        R->>AI: Emoji celebration message
        R->>AI: AI-generated celebration image
        R->>HA: Play victory music
        R->>SMS: Text significant other
    end

    AI->>U: "CRUSHED IT! 🔥💪✨ [unique AI celebration image]"

    opt High intensity + cleared schedule
        R->>AI: Outing suggestion
        AI->>U: "You've earned it - coffee at Luna Cafe? ☕"
    end
```

### State Diagram Update

```mermaid
stateDiagram-v2
    [*] --> Pending: Task created

    Pending --> InProgress: User accepts
    InProgress --> Completed: User finishes

    Pending --> InitiationReward: User accepts (initiation trigger)
    InProgress --> InitiationReward: First step done / Resumed

    InitiationReward --> RewardEvaluation: Calculate score (weighted + capped)
    Completed --> RewardEvaluation: Calculate score (full)

    RewardEvaluation --> RewardDelivery: Map score to intensity level

    state RewardDelivery {
        [*] --> Emoji
        Emoji --> Image: if enabled + score ≥ Medium
        Image --> Music: if enabled + score ≥ High
        Music --> TextSO: if enabled + score ≥ High
        TextSO --> Outing: if score = Epic
        Outing --> [*]
    }

    InitiationReward --> InProgress: Continue working
    RewardDelivery --> [*]: All rewards delivered
```

---

## Agent Commands

Capabilities exposed via conversation commands, not HTTP endpoints. OpenClaw agent handles directly.

| Command | Purpose |
|---------|---------|
| Reward settings | Get or update current reward settings |
| Test music | Test music integration |
| Test SMS | Test SMS delivery |
| Reward history | Get recent reward history |
| Home status | Check home system connectivity |
| List rooms | List available rooms |
| Play music | Trigger music playback |
| Stop music | Stop current playback |

---

## Environment Variables

| Variable | Purpose | Example |
|----------|---------|---------|
| `OPENAI_API_KEY` | OpenAI API for image generation | `sk-proj-xxxxxxxx` |
| `TWILIO_ACCOUNT_SID` | Twilio authentication | `ACxxxxxxxx` |
| `TWILIO_AUTH_TOKEN` | Twilio authentication | `xxxxxxxx` |
| `TWILIO_PHONE_NUMBER` | Sender phone number | `+1234567890` |
| `SONOS_API_KEY` | Sonos integration | `xxxxxxxx` |
| `HOME_ASSISTANT_URL` | Home Assistant endpoint | `http://ha.local:8123` |
| `HOME_ASSISTANT_TOKEN` | Home Assistant auth | `xxxxxxxx` |
| `OPENWEATHER_API_KEY` | Weather for outings | `xxxxxxxx` |

---

## Implementation Phases

```mermaid
gantt
    title Reward System Implementation
    dateFormat  X
    axisFormat %s

    section Phase 1: Core
    Emoji celebrations           :done, p1a, 0, 1
    Basic completion messages    :done, p1b, 0, 1

    section Phase 2: AI Images
    Image generation script      :done, p2a, 1, 2
    Intensity-based themes       :done, p2b, 1, 2
    Streak visual enhancements   :done, p2c, 1, 2

    section Phase 3: Audio
    Home audio integration       :p3a, 2, 3
    Music preference config      :p3b, 3, 4

    section Phase 4: Social
    SMS notifications            :p4a, 4, 5
    Multi-platform messaging     :p4b, 5, 6

    section Phase 5: Novelty
    Seasonal theme injection     :p5a, 6, 7
    Milestone surprises          :p5b, 6, 7
    Preference learning          :p5c, 7, 8

    section Phase 6: Polish
    Personalized outings         :p6a, 8, 9
    A/B testing framework        :p6b, 9, 10
```

---

## Success Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| Task completion rate | +20% | Compare before/after |
| Session duration | +15% | Average time in app |
| Return rate | +25% | Users returning within 24h |
| Streak length | +30% | Average consecutive completions |
| User satisfaction | 4.5/5 | Post-session survey |
| Reward engagement | 80%+ | Rewards not dismissed immediately |