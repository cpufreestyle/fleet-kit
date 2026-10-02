# Code model selection for FleetKit (measured)

Which model should FleetKit use to write and edit FleetKit itself -- the
bridges, the launchd glue, this tooling. Answered from measured ground truth,
not vibes: real coding tasks, real pytest, real seconds, and a count of wasted
prose.

**TL;DR -- use `workbuddy/deepseek-v4-flash` as the coding model.**

## Method (why this is ground truth)

- Two coding tasks, both drawn from FleetKit real code paths:
  - Task 1 repair `parse_window` (free-window parser, 8 checks).
  - Task 2 implement `convert_anthropic_to_openai` (the core request-body
    translation, 7 checks).
- A model reply is pulled down to its fenced code block, that block is run
  against a real testsuite in the runtime venv, and passes are counted. Speed
  and prose (everything outside the fence) are recorded too, so a model cannot
  win by being chatty.
- Harness: `kit/tools/code_model_bench.py`.
  - `code_model_bench.py offline` grades known-good and known-garbage fixtures
    with no network and must print `OFFLINE_SELFTEST PASS`.
  - `code_model_bench.py grade <raw>` grades a saved reply.
  - `code_model_bench.py live <model...> [--bridge NAME]` measures a live model.
- The two reference answers ship in the file and score 8/8 and 7/7 with zero
  prose, which is the proof the grader itself is honest.

## Measured results

| model | tier / route | task1 (parse_window) | task2 (convert) | seconds | prose | verdict |
| --- | --- | --- | --- | --- | --- | --- |
| `workbuddy/deepseek-v4-flash` | workbuddy | 8/8 | 7/7 | 2-4 | 0 | **primary** |
| `workbuddy-gpt/hy4-preview` | workbuddy / opus | strong | strong | >9 | low | real power, too slow to iterate |
| `stepfun/step-5-preview` | harbor (boot default) | - | - | >9 | - | too slow for code loops |
| `workbuddy/deepseek-v4-pro` | workbuddy | strong* | - | 2.7 avg* | low | off-by-one on long files, not the coding default |
| `workbuddy/glm-5.2`, `glm-5.1` | workbuddy | full | full | fast | low | excluded by user for this repo |
| `minimax`, `qoder`, `stepfun` | various | - | - | - | - | excluded by user for this repo |

DeepSeek-V4-Pro full marks came from the 2026-10-02 60-call real run (Chinese
RAG QA plus code generation), where it and MiniMax-M3 led the field and
Codely-Core faltered on 100s timeouts.

## Conclusion

- **Primary coding model: `workbuddy/deepseek-v4-flash`.** Full marks on both
  tasks, fastest round trip (2-4s), zero wasted prose -- built for the tight
  edit-run loop this repo lives in.
- **Heavy single edits: `workbuddy-gpt/hy4-preview`** (the gateway opus and
  Claude alias already). Strong work but over 9s a turn, so keep it for the
  opus tier and interactive chat, not for iterating on this code.
- **Not used for FleetKit coding, by user preference:** `glm-5.1`, `glm-5.2`,
  `minimax`, `stepfun` (step-5), `qoder`.
- **`deepseek-v4-pro` is not the coding default** despite its speed: the user
  saw off-by-one behavior on long files. Use flash for code; pro only where
  its extra strength is worth the risk and the edits are small.

## Refreshing the tables

```bash
cd kit/tools
# gateway route (needs the gateway token set)
FLEET_GATEWAY_URL=http://127.0.0.1:8801 FLEET_ANTHROPIC_TOKEN=... \
  ../../runtime/.venv/bin/python code_model_bench.py live \
  workbuddy/deepseek-v4-flash workbuddy-gpt/hy4-preview

# direct-bridge route (key comes from runtime/fleet.env)
../../runtime/.venv/bin/python code_model_bench.py live \
  workbuddy/deepseek-v4-flash --bridge workbuddy
```

After account recovery (Gemini VALI, Trae quota, Qwen key), re-run `live` to
fold those bridges back into the tables above.

## Related settings (do not change silently)

- `FLEET_DEFAULT_MODEL` stays `stepfun/step-5-preview`. This document is about
  which model a human or agent should reach for when editing FleetKit; it does
  not change the gateway boot default. Changing the boot default is its own,
  separate decision.
- Token/file-size note from the 2026-10-02 run: keep `max_tokens` at 16000 or
  more where possible or the context burns fast; `deepseek-v4-pro`/`flash` live
  on the ocx gateway (port 10100), a different network from local 4002, so they
  are not subject to the local 2000-token hard cap.

## Files

- `kit/tools/code_model_bench.py`
- `kit/tools/test_code_model_bench.py`
- `kit/docs/code-model-selection.md` (this file)
