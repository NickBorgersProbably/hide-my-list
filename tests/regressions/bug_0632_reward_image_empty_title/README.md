# Bug 0632: Reward images blocked by empty task titles

**Issue:** #632

## Bug Story

Completion rewards fell back to text whenever the active task title was empty.
The image prompt does not include task descriptions, so filtering blank
descriptions and treating an empty list as fatal blocked image generation
without protecting private data.

## Fix

- Intake treats a blank model-supplied title as absent and falls back to the
  user's message.
- Selection omits blank active-task titles instead of preserving them.
- Complete treats a blank active-task title as absent before calling rewards.
- `generate_reward_image()` no longer treats an empty private description list
  as fatal; intensity remains the image gate.

## Regression Tests

Tests live in this directory:

- `test_reward_image_empty_title.py` verifies intake title fallback, selection
  active-task title omission, complete-node reward fallback title handling, and
  direct reward image generation with blank descriptions.
