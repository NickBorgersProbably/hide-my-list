# Bug 0687: Reward Copy Asserts a List State

**PR:** #687

## Bug Story

An epic-intensity completion celebrated with "INBOX ZERO! 🏆👑✨🎉🔥💪🚀" while
the user still had open tasks. "PROJECT COMPLETE! 🚀⭐💪🎊" sat in the same set.
The reward layer scores a completion from the task's time estimate, energy,
the streak, and recent reward frequency. It does not know whether the list is
empty or a larger project is finished, so any template that states such a fact
is false whenever the draw lands on it.

`compute_intensity` and `maybe_reward` accept `is_parent_complete` and
`is_all_cleared`, but no caller passes them, so epic is reached only by a long,
high-energy task on a streak — never by a cleared list.

## Fix

- `app/tools/rewards.py`: "INBOX ZERO!" became "HUGE! 🏆👑✨🎉🔥💪🚀" and
  "PROJECT COMPLETE!" became "THAT WAS A BIG ONE! 🚀⭐💪🎊". Emoji and the number
  of templates per intensity are unchanged.
- `docs/reward-system.md`: the completion template table is indexed by
  intensity and describes what actually reaches each one; the state rule says
  template text never asserts a fact about the list or a project.

## Regression Tests

- `test_reward_copy_asserts_no_list_state.py`: no template at any intensity
  states a list or project fact, and the epic draw never returns one.
- Unit: `tests/unit/test_rewards.py`
  (`test_no_template_asserts_list_or_project_state`).
