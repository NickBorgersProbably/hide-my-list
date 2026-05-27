# Bug 0563: Reward Image Orphaned from Delivery

**Issue:** (no separate issue filed; PR #563 is the canonical reference)
**Fix PR:** #563

## Bug Story

`generate_reward_image` saved celebration PNGs and wrote manifests to the
reward_manifests table. However, `signal_client.send_message` was called as
text-only -- the generated image was never attached to the outbound Signal
message. The image existed on disk but was invisible to the user.

The integration tests at the time verified that `send_message` was called
(`mock.assert_called()`), but did NOT assert that the call included an
`attachment_path` kwarg. This is bug class 3: integration tests asserting
`mock.called` instead of `mock.call_args.kwargs` shape.

## Regression Tests

The regression test asserts that when `complete_node` fires the reward path
and generates an image, the `signal_send_fn` call includes a non-None
`attachment_path` in its kwargs. The assertion is on the SHAPE of the call,
not just the fact of the call.

A full regression test requires driving the complete node with a mocked LLM
that returns a reward decision that triggers image generation. This requires
either a real reward decision mock or integration setup with the graph.

**The test lives in** `tests/integration/test_reward_image_delivery.py` (to be
added in a follow-up PR). This README documents where it should live and what
it must assert:

- Drive `complete_node` with a mocked LLM returning a reward decision
  that triggers image generation.
- Capture `signal_send_fn.call_args.kwargs`.
- Assert `attachment_path` is present and non-None.
- Assert the file at `attachment_path` exists on disk.
- Assert the `reward_manifests` row is marked as delivered.
