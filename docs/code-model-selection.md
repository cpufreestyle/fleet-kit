# Code model selection for FleetKit (measured)

Which model should FleetKit use to write and edit FleetKit itself -- the
bridges, the launchd glue, this tooling. Answered from measured ground truth,
not vibes: real coding tasks, real pytest, real seconds, and a count of wasted
prose.

**TL;DR -- use `workbuddy/deepseek-v4-flash` as the coding model.** It is
also the boot default as of 2026-10-03 (`FLEET_DEFAULT_MODEL`), so Codex
opens on a model measured to write this repo, not on the harbor.

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

### 2026-10-02 evening re-run (all 15 bridges listed in /v1/models)

`code_model_bench.py live` over the same two tasks, plus one retry pass so a
transient blip is not mistaken for a dead route:

| model | bridge | task1 | task2 | seconds | result |
| --- | --- | --- | --- | --- | --- |
| `deepseek-v4-flash` | workbuddy | 8/8 | 7/7 | 7.0-8.3 | **primary, three consecutive full-mark runs** |
| `deepseek-v4-pro` | workbuddy | 8/8 | 7/7 | 9.6 | full marks on both standard tasks too |
| `hy4-preview` | workbuddy-gpt | - | - | - | smoke 429 rate-limited, both attempts |
| `kimi/kimi-for-coding` | kimi | - | - | - | smoke 403 (account/key) |
| `trae/seed-code-pro-0430` | trae | - | - | - | smoke 502 upstream |
| `cline-free/deepseek-v4.1-flash` | cline | - | - | - | smoke 502 upstream |
| `stealth/pixel-canary` | cline | - | - | - | smoke 502 upstream |
| `claude-opus-4-8@default` | antigravity | - | - | - | smoke 502 upstream |
| `gemini-3-pro-preview` | gemini | - | - | - | smoke 502 upstream |
| `gemini-3-flash-preview` | gemini | - | - | - | smoke 502 upstream |
| `qwen/qwen3.8-max` | qwen | - | - | - | smoke 503 |
| `xhx/sn-deepseek-v4-1-flash` | xhx | 8/8 | NO_RUNNABLE | 17.1 | convert not runnable |
| `lingxi/deepseek-flash` | lingxi | - | - | - | 429 on task1 |
| `codely-core` | codely | NO_RUNNABLE | NO_RUNNABLE | ~13 | both tasks unusable |
| `GLM-5.3` | zcode | - | - | - | smoke 503 (22s slow fail) |

The same error code on the retry pass means route state, not a blip. Note the
panel's /v1/models answers 200 for these bridges: the model catalogue is
served locally and only the chat call goes upstream, so a green dashboard
row does not mean a usable code model. re-run `live` on any row above once
its bridge recovers.

## Conclusion

### 2026-10-03 full-fleet re-run (21 models, every bridge in /v1/models)

`code_model_bench.py live` over the same two tasks, one pass per model. This is
the pass that decided the boot default, so it covered the whole fleet rather
than the bridges that looked healthy the evening before:

| model | bridge | task1 (parse_window) | task2 (convert) | seconds | prose | result |
| --- | --- | --- | --- | --- | --- | --- |
| `deepseek-v4-flash` | workbuddy | 8/8 | 7/7 | 7.5 | 0 | **primary** |
| `deepseek-v4.1-flash` | workbuddy | 8/8 | 7/7 | 6.4 | 0 | full marks, fastest |
| `deepseek-v4-pro` | workbuddy | 8/8 | 7/7 | 9.1 | 0 | full marks again |
| `DeepSeek-V4-Pro` | qoder | 8/8 | 7/7 | 36.8 | 0 | full marks, ~5x slower; excluded by user preference |
| `trae/seed-code-pro-0430` | trae | 8/8 (32.1s) | timeout at 45s | 83.0 | 0 | task1 passes, task2 too slow |
| `hy4-preview` | workbuddy-gpt | - | - | - | - | smoke 429 rate-limited |
| `gpt-5.3-codex` | workbuddy-gpt | - | - | - | - | smoke 503 |
| `hy3` | workbuddy-gpt | - | - | - | - | smoke 503 |
| `xhx/sn-deepseek-v4-1-flash` | xhx | NO_RUNNABLE | NO_RUNNABLE | 15.0 | 337 | chat answers, never a code fence |
| `xhx/sn-glm-5-3` | xhx | NO_RUNNABLE | NO_RUNNABLE | 38.4 | 3 | same |
| `xhx/sn-kimi-k3` | xhx | NO_RUNNABLE | NO_RUNNABLE | 38.5 | 1071 | same |
| `cline-free/deepseek-v4.1-flash` | cline | - | - | - | - | smoke 502 |
| `stealth/pixel-canary` | cline | - | - | - | - | smoke 502 |
| `lingxi/deepseek-flash` | lingxi | - | - | - | - | 429 on task1 |
| `codely-core` | codely | NO_RUNNABLE | NO_RUNNABLE | 11.2 | 0 | both tasks unusable |
| `kimi/kimi-for-coding` | kimi-code | - | - | - | - | smoke 503 |
| `qwen/qwen3.8-max` | qwen | - | - | - | - | smoke 503 |
| `gemini-3-flash-preview` | gemini | - | - | - | - | smoke 502 |
| `GLM-5.3` | zcode | - | - | - | - | smoke 503 |
| `claude-sonnet-4-5@20250929` | antigravity | - | - | - | - | smoke 502 |
| `deepseek-v3.2` | catpaw | - | - | - | - | smoke 502 |

Three findings the evening run could not show:

- **Only workbuddy's deepseek route writes this repo reliably.** Four models
  finished both tasks (the workbuddy three plus qoder's DeepSeek-V4-Pro), but
  qoder took 36.8s against flash's 7.5s and the user excludes qoder here
  anyway. Every other bridge either failed its smoke call or answered prose
  where the harness needs a fenced block.
- **`xhx` looks alive and is not usable.** All three xhx models reach and reply
  in 15-39s, yet none produce a runnable block. A listening port and a real
  answer are not the same thing as a model that can edit code.
- **The live default had drifted onto one of those dead ends.**
  `~/.codex/config.toml` carried `model = "xhx/xhx-sn-deepseek-v4-1-flash"`, a
  NO_RUNNABLE route. `default_model_guard.py` did not flag it, correctly: that
  route does answer a chat call, and the guard owns route health, not whether
  the model behind it can produce code. That gap is why this document exists.

The re-run is what promoted flash to the boot default. `FLEET_DEFAULT_MODEL`
now names `workbuddy/deepseek-v4-flash`, `~/.codex/config.toml` pins the same
slug, and both the guard and an end-to-end call through the gateway (10100,
responses API, `PROXY_MANAGED`) answer: all three workbuddy deepseek models
return `E2E_OK` in 1.4-1.7s.

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

**2026-10-02 evening update:** pro also scored 8/8 + 7/7 on the standard
tasks, so the off-by-one note is about long files the user edited by hand,
not the task suite. flash remains the default for the tight edit-run loop;
pro is the validated fallback when flash is rate-limited, as it was for
hy4-preview and most external bridges that evening.

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

- `FLEET_DEFAULT_MODEL` is `workbuddy/deepseek-v4-flash` since the 2026-10-03
  re-run above: the model measured to write this repo is now the model Codex
  boots on, and the harbor (`stepfun/step-5-preview`) stays the failover target
  when that route dies. Changing the boot default again is its own decision --
  edit `fleet.env`, re-pin `~/.codex/config.toml` with the same line transform
  `setup-providers.sh` uses, then re-run `tools/default_model_guard.py`.
- Token/file-size note from the 2026-10-02 run: keep `max_tokens` at 16000 or
  more where possible or the context burns fast; `deepseek-v4-pro`/`flash` live
  on the ocx gateway (port 10100), a different network from local 4002, so they
  are not subject to the local 2000-token hard cap.

## Files

- `kit/tools/code_model_bench.py`
- `kit/tools/test_code_model_bench.py`
- `kit/docs/code-model-selection.md` (this file)
