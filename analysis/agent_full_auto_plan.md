# AgentCli Architecture Analysis & Full-Automation Roadmap

This document analyzes your current AgentCli codebase and proposes a concrete plan to evolve it toward a “全自动软件需求开发” (fully automated software requirements-to-code pipeline).

---

## 1. What key flows are already implemented?

### 1.1 CLI → Orchestrator entrypoints

**File:** `agent/cli.py`

- `build_parser()` defines the CLI surface:
  - `models` – list available LLM models (via HTTP to OpenRouter/OpenAI-compatible endpoints).
  - `plan` – generate and display an execution plan.
  - `do` – execute a task end-to-end (multi-iteration).
  - `rollback` – rollback to last git checkpoint.
  - `resume` – resume last run.
  - `ui` – start a simple Web UI.
- `main()`:
  - Resolves `repo_root = Path.cwd()`.
  - Instantiates:
    - `RunManager(repo_root)`
    - `ToolRouter(repo_root)`
    - `Orchestrator(repo_root, run_manager, tool_router, build_only=..., env_overrides=...)`
  - Dispatches CLI commands:
    - `plan` → `Orchestrator.plan_only(...)`
    - `do` → `Orchestrator.run(...)`
    - `resume` → `Orchestrator.run(..., resume=True)`
    - `rollback` → `Orchestrator.rollback()`
    - `ui` → `web_ui.run_server(...)`

**Key takeaway:**  
You already have a single, clean entrypoint that creates the orchestration and routing layer. Extending the CLI with new “requirements” modes will be straightforward.

---

### 1.2 Orchestrator: multi-iteration loop

**File:** `agent/orchestrator.py`

Core responsibilities:

- **Run lifecycle management:**
  - `plan_only(task, as_json, auto)`:
    - Calls `run_manager.create_run(...)`.
    - Creates a git checkpoint via `tool_router.git_checkpoint(...)`.
    - Builds a plan via `_build_plan(task)` and saves it.
  - `run(task, auto, resume=False)`:
    - If `resume`: loads state, otherwise:
      - `create_run()`, `git_checkpoint()`, `_build_plan()`, `_prompt_plan()`.
    - Creates `RunContext` via `_make_context(state, auto)`.
    - Instantiates a `PipelineRunner` via `_make_pipeline()`.
    - Executes loop:

      ```text
      Stage.PREPARE
        -> check env_decision
        -> optional user confirmation

      for each iteration:
        Stage.GATHER
        Stage.EDIT
        Stage.APPLY (via _apply_patches)
        Test-coverage policy check
        Stage.VERIFY_BUILD
        Stage.VERIFY_TEST
        interactive decisions or auto mode
      ```

    - Emits events for error/success via `EventBus`.

- **Environment & context:**
  - `_make_context()` builds a `RunContext` with:
    - `task`, `workspace`, `run_dir`, options, policy.
    - `tool_router`, `run_manager`, `events`, `skills`, `iteration`.
    - `services={"llm": LLMService.from_env()}`.
    - `file_contents={}` for editing executor.

- **Pipeline setup:**
  - `_make_pipeline()`:
    - Registers agents in `AgentRegistry`:
      - `Stage.PREPARE` → `EnvAgentPlugin`
      - `Stage.GATHER` → `RepoScoutPlugin`
      - `Stage.EDIT` → `PatchAuthorPlugin`
      - `Stage.VERIFY_BUILD` → `BuildPlugin`
      - `Stage.VERIFY_TEST` → `TestPlugin`

- **Patch application:**
  - `_apply_patches(ctx)`:
    - Reads patch JSON files from `ctx.patch_queue`.
    - Uses `parse_request` + `EditExecutor` to apply edits.
    - Tracks `ctx.applied_files` and emits `apply.diff` events.

- **Policy: test coverage enforcement:**
  - `_check_test_coverage_needed(ctx)`:
    - If code files changed but no test files in `ctx.applied_files`:
      - Returns `(True, reason)` and sets `ctx.policy["need_tests"] = True`.
    - Drives behavior in the loop: extra iterations to add tests.

- **Hints gathering for LLM:**
  - `_collect_hints(ctx)`:
    - Based on last build/test summaries; hints fed into RepoScout.

**Key takeaway:**  
You already have a multi-iteration, policy-aware feedback loop orchestrated through stages. It’s very close to a general-purpose “agent pipeline” and is reusable for full automation, especially if we add requirements/design stages and reduce interactive prompts.

---

### 1.3 Agent framework & pipeline

**Files:**  
- `agent/framework/agent_types.py`
- `agent/framework/pipeline.py`
- `agent/framework/registry.py`

**AgentTypes & stages:**

- `Stage` enum includes:
  - `PLAN`, `PREPARE`, `GATHER`, `EDIT`, `APPLY`, `VERIFY_BUILD`, `VERIFY_TEST`, `REVIEW`, `FINALIZE`.
  - Note: several stages currently unused (`PLAN`, `APPLY`, `REVIEW`, `FINALIZE`), giving room for expansion.
- `AgentResult`:
  - `status`: `"ok" | "warn" | "fail" | "skip"`.
  - `events`, `artifacts`, `outputs`, `suggest_next`.
- `Agent` Protocol:
  - `id`, `stage`, `priority`, `run(ctx, request=None) -> AgentResult`.

**Pipeline & registry:**

- `AgentRegistry`:
  - `register(stage, agent, priority=0)` with priority ordering.
  - `get(stage)` returns agents in sorted order.
- `PipelineRunner.run_stage(stage, ctx, request=None)`:
  - Emits `stage.enter`, `stage_start`.
  - Runs each agent, collects `AgentResult`s.
  - Emits `agent_start/agent_end`, additional events from `AgentResult.events`.
  - Stops early on `status == "fail"`.
  - Emits `stage_end` and `stage.exit`.

**Key takeaway:**  
You have a clean agent plug-in architecture that is already stage-based. To reach “requirements-to-code” automation, we mainly need to **add new agents and possibly use more stages**, not redesign the framework.

---

### 1.4 Skills and tool routing

**File:** `agent/skills/registry.py`

- `SkillRegistry`:
  - Registers skills by id.
  - Provides `run(skill_id, ctx, **kwargs) -> SkillResult`.
- Orchestrator creates skills in `_make_skills()` using:
  - `SearchSkill`, `ReadFileSkill`, `RunCommandSkill` (from `.skills` package).
- Many agents use `ctx.skills.run("run_command", ...)` etc. as abstraction over `ToolRouter`.

**Key takeaway:**  
Skills already provide an extendable “tool” mechanism. We can add new skills to manipulate a **requirements store**, **design artifacts**, or **traceability graphs** without touching the core orchestrator.

---

### 1.5 Environment detection and build command selection

**Files:**  
- `agent/env_agent.py`
- `agent/agents/env_agent_plugin.py`

**EnvAgent:**

- Accepts `EnvRequest` with workspace, preferred build/test commands, interactive preferences, overrides.
- `decide(req) -> EnvDecision`:
  - Detects platform (windows/mac/linux).
  - Detects make/nmake, WSL, compilers, python, workspace features.
  - Applies overrides:
    - `override_make_cmd`
    - `force_strategy` (`wsl` or `fallback`)
    - `override_use_wsl`
  - Chooses strategy:
    - `gnu_make` (preferred, using `make`, `gmake`, or `mingw32-make`).
    - `nmake` on Windows.
    - `wsl_make` if WSL is available and allowed.
    - `fallback_py` using `build.py` when no make is found.
    - or an `error` strategy.
  - Returns `EnvDecision` with:
    - `commands`: build/test command strings.
    - `detections`, `fallback`, `user_actions`, `warnings`.

**EnvAgentPlugin:**

- Stage: `PREPARE`.
- Builds `EnvRequest` from `RunContext`.
- Calls `EnvAgent.decide()`, stores decision in `ctx.env_decision`, saves JSON, emits events.
- Returns `AgentResult` with status `ok` / `fail`.

**Key takeaway:**  
The environment-selection and build/test command resolution is already automated and robust enough for a full auto pipeline. Reuse as-is; only minor adjustments may be needed as we add new build/test strategies.

---

### 1.6 Repository scouting / context harvesting

**File:** `agent/agents/reposcout_plugin.py`

- Stage: `GATHER`.
- Rebinds `RepoScout` with current `tool_router`/`run_manager`.
- Calls `self.agent.gather(ctx, hints=request or [])`.
- Puts result into `ctx.context_pack`, saves JSON, emits `gather.summary`.

While we don’t see `RepoScout` implementation here, we know:

- It likely searches codebase for relevant files based on task/hints.
- `ctx.context_pack` is later used by `PatchAuthorPlugin` to collect allowed files and provide file content context to the LLM.

**Key takeaway:**  
You already have an automated context-gathering mechanism that feeds the editing stage. This is critical for requirement-to-code workflows, and can be extended to also gather spec/requirements context.

---

### 1.7 LLM service & patch generation

**Files:**  
- `agent/llm/service.py`
- `agent/agents/patch_author_plugin.py`

**LLMService:**

- `_load_provider_from_env()`:
  - Selects provider based on `AGENT_LLM_PROVIDER` (`openai_compat`, `ollama`, `agent_cli`).
  - Handles base_url + api_key; normalizes OpenRouter URL.
- `LLMService.__init__`:
  - Reads model/timeout/max_tokens/temperature/max_retries from env.
- `LLMService.enabled()`:
  - Returns true if provider configured.
- `generate_patch(messages: List[ChatMessage]) -> Dict[str, any]`:
  - Wraps call to provider `.generate(...)`.
  - Retries up to `max_retries`.
  - Validates response via `_is_valid_response()`:
    - Accepts either:
      - unified diff (`diff --git`) or
      - JSON (list or dict).
  - Returns structured dict: `ok`, `content`, `latency_ms`, `usage`, `attempt` or error.

**PatchAuthorPlugin:**

- Stage: `EDIT`.
- Binds `PatchAuthor` agent with `tool_router` & `run_manager`.
- Fetches `llm` from `ctx.services["llm"]`.
- Emits detailed events about LLM configuration & call.
- If no LLM → `status="skip"` with note.
- Builds `allowed_files` via `_collect_allowed_files(ctx)` based on `ctx.context_pack` or a default list (demo C project).
- `_build_prompt(ctx, allowed_files)`:
  - Builds a strict **system prompt** describing:
    - Output must be **raw JSON** (no markdown).
    - Strict editing protocol (action/file_path/edits).
    - Exact match requirements for `old_string`.
    - Allowed files, pre-flight checks, minimal changes.
  - Builds user message:
    - Task description.
    - Optional requirement: must add/update tests when `ctx.policy["need_tests"]` is true.
    - Diagnostics from last build/test.
    - File contexts (bounded; truncated for long files).
- Calls `llm.generate_patch(prompt_msgs)` with retry logic for:
  - LLM errors.
  - JSON parsing errors (`_parse_edits()`).
  - Protocol normalization errors (`_normalize_protocol_payload()`).
  - Edit verification via `EditExecutor.apply(dry_run=True)`.
- On success:
  - Saves JSON payload via `run_manager.save_patch`.
  - Enqueues patch path in `ctx.patch_queue`.
  - Emits `patch.proposed`.
  - Returns `AgentResult(status="ok", artifacts=[...], outputs={"payload": final_payload})`.

Utility methods:

- `_read_file_content()` – uses `ReadFileSkill` if available, caches `ctx.file_contents`.
- `_build_diagnostics_block()` – formats last build/test summary for the prompt.
- `_parse_edits()` – strips markdown fences and loads JSON.
- `_normalize_protocol_payload()` – adapts legacy shapes (lists, search_block/replace_block) into `action/file_path/edits` protocol.
- `_validate_edits()` – (currently not wired into main flow) checks allowed files and uniqueness of search_block.
- `_collect_allowed_files()` / `_extract_files_from_context()` – derive allowed file list from `ctx.context_pack` or default.

**Key takeaway:**  
You already have a relatively sophisticated **LLM-driven patch authoring agent**, with good protocol validation and dry-run verification. This is a strong foundation for automatic code editing. For “requirements development”, we will add parallel agents for **requirements** and **design** phases that use a similar pattern.

---

### 1.8 Build and test triage agents

**Files:**  
- `agent/build.py`, `agent/agents/build_plugin.py`
- `agent/tester.py`, `agent/agents/test_plugin.py`

**BuildDiagnoser & BuildPlugin:**

- `BuildDiagnoser.run(ctx, build_cmd, cwd)`:
  - Uses `_run_command` via skills or `tool_router`.
  - Saves build log with `run_manager.save_verify_log`.
  - Parses errors from stderr via `_parse_errors()` (GCC/Clang style).
  - Returns dict: `success`, `log`, `raw`, `summary`.
- `BuildPlugin.run(ctx, request=None)`:
  - Picks `build_cmd` from `ctx.env_decision["commands"]["build"]`.
  - Emits events, runs diagnoser, saves JSON to `build_<iteration>.json`.
  - Returns `AgentResult` with status ok/fail, outputs & artifacts.

**TestTriage & TestPlugin:**

- `TestTriage.run(ctx, test_cmd, cwd)`:
  - Uses `_run_command` similarly.
  - Saves test log as `test` verify log.
  - Parses JUnit XML (`build/tests/report.xml`) or stdout for GoogleTest-style failures.
  - Returns `success`, `log`, `raw`, `summary` where summary lists failing test suites/cases.
- `TestPlugin.run(ctx, request=None)`:
  - Picks `test_cmd` from `ctx.env_decision["commands"]["test"]`.
  - Injects `TEST_SHOULD_FAIL=0` into known WSL/bash patterns to defeat demo-intentional failures.
  - Runs triage, saves JSON `test_<iteration>.json`.
  - Emits events and returns `AgentResult`.

**Key takeaway:**  
Build and test stages are already robust, producing structured summaries that feed into hints for RepoScout and diagnostics for PatchAuthor. They will remain core components in a fully automated pipeline.

---

## 2. Which components are directly reusable for a “full auto requirements development” pipeline?

### 2.1 Reusable as-is (little to no change)

- `agent/cli.py`:
  - CLI structure and `do/plan/resume/rollback` commands.
  - For full automation, we add new options/subcommands, but existing logic is good.
- `agent/framework/agent_types.py`:
  - `Agent`, `AgentResult`, `Stage` (with additional stages already present).
- `agent/framework/pipeline.py`, `agent/framework/registry.py`:
  - Pipeline runner and registry; ideal plugin infrastructure for new requirement/design agents.
- `agent/env_agent.py`, `agent/agents/env_agent_plugin.py`:
  - Environment detection and strategy selection.
- `agent/build.py`, `agent/agents/build_plugin.py`:
  - Build execution and error parsing.
- `agent/tester.py`, `agent/agents/test_plugin.py`:
  - Test execution and failure parsing.
- `agent/skills/registry.py`:
  - Skill registration and dispatch.
- `agent/llm/service.py`:
  - Provider configuration and generic `generate_patch` infrastructure; we can add more methods but the core structure stays.

### 2.2 Reusable with moderate extension

- `agent/orchestrator.py`:
  - The iterative loop, hints, event flushing, test-coverage policy.
  - Will be extended to:
    - Add **requirements** and **design** pipeline stages.
    - Reduce human interactions for full-auto modes.
    - Manage new artifacts (requirements/spec documents) in `RunContext`.
- `agent/agents/reposcout_plugin.py`:
  - Already gathers context; we may:
    - Extend `RepoScout` to be aware of requirements and design artifacts.
    - Provide specialized context flows for new stages.
- `agent/agents/patch_author_plugin.py`:
  - We can:
    - Allow configuration for different prompting “modes” (bug-fix vs feature/requirement).
    - Accept a structured “spec” object from upstream agents.
    - Possibly generate patches for tests only vs code only, depending on policy.

### 2.3 New components needed

To achieve “全自动软件需求开发”, we’ll add:

1. **Requirements Agent** (new files)
   - Stage: `Stage.PLAN` or possibly a new `Stage.REQUIREMENTS`.
   - Responsibilities:
     - Take `task` (natural-language requirement) + repo context.
     - Produce:
       - A structured requirement spec (JSON schema).
       - Acceptance criteria.
       - Initial test plan.
   - Implementation:
     - Use `LLMService` with a new method `generate_requirements(messages)` or reuse `generate_patch` but with a different validation schema.
     - Save artifacts via `RunContext` (e.g., `ctx.save_json("requirements", spec)`).

2. **Design Agent**
   - Stage: `Stage.REVIEW` or new `Stage.DESIGN`.
   - Responsibilities:
     - Transform requirements into:
       - High-level design (components, modules).
       - Traceability mapping: requirement → modules/files/tests.
     - Provide “design context” for PatchAuthor and RepoScout.

3. **Test-Generation Agent**
   - Stage: between `EDIT` and `VERIFY_TEST` (or integrated into `PatchAuthor` behavior).
   - Responsibilities:
     - Given a requirement + design + diff history:
       - Propose new tests or updates to existing tests.
     - Could share infrastructure with PatchAuthor since test changes are just additional patches.

4. **Traceability/Memory Store (Skill)**
   - New `Skill` implementations for:
     - Reading/writing structured artifacts to a persistent store under the run dir (e.g., `requirements.json`, `design.json`).
     - Searching them by requirement id, etc.
   - This can be implemented using the `SkillRegistry` and `RunContext` utilities.

---

## 3. Suggested refactor & development plan

Below is a pragmatic, staged roadmap. Importantly, **stage by stage you can still run the existing demo** while gradually evolving toward full automation.

### Phase 1: Add a “requirements-aware” layer on top of the current pipeline

**Goal:** Keep existing build/test/edit loop intact, but introduce structured requirements & design artifacts that the LLM can see.

1. **Introduce requirement & design artifacts in RunContext**
   - Add fields to `RunContext` (in its module, not shown here) such as:
     - `requirements: Dict[str, Any] | None`
     - `design: Dict[str, Any] | None`
   - Add save/load helpers if necessary (RunContext already has `save_json`).

2. **Add a RequirementsAgent (no pipeline integration yet)**
   - New file: `agent/agents/requirements_plugin.py`.
   - Similar structure to `PatchAuthorPlugin`:
     - Stage: `Stage.PLAN` or a new stage `Stage.REQUIREMENTS` (requires adding to `Stage` enum).
     - Uses `LLMService` to produce a JSON spec for the `task`:
       - Requirements list.
       - Acceptance criteria.
   - Initially, just a callable you can invoke manually from a small script or a new CLI subcommand (e.g., `agent requirements "task"`).

3. **Add a DesignAgent**
   - New file: `agent/agents/design_plugin.py`.
   - Stage: `Stage.PLAN` or `Stage.REVIEW`.
   - Input: `ctx.requirements` + `ctx.context_pack`.
   - Output: `ctx.design` JSON:
     - Components, target files/modules.
     - Proposed test suites.
   - For now, only generates design documents; doesn’t yet affect PatchAuthor.

4. **Extend PatchAuthor to read requirements/design**
   - Without changing its core editing logic, adjust `_build_prompt` to optionally include:
     - Requirements summary.
     - Design summary.
   - Keep existing diagnostics-first flow; just add requirement/design context at top.

**Result:**  
You get a **requirements-aware code-editing pipeline**: still triggered from `agent do "task"`, but patch generation now sees structured requirements and design, increasing coherence.

---

### Phase 2: Integrate new agents into the orchestrator pipeline

**Goal:** Use the existing stage-based pipeline to automatically derive requirements/design before code edits.

1. **Enable new stages in `_make_pipeline()`**
   - If you added `Stage.REQUIREMENTS` and/or `Stage.DESIGN` to `Stage` enum:
     - Register:
       - `reg.register(Stage.REQUIREMENTS, RequirementsAgentPlugin())`
       - `reg.register(Stage.DESIGN, DesignAgentPlugin())`
   - Alternatively, reuse `Stage.PLAN` and `Stage.REVIEW`.

2. **Extend `Orchestrator.run()` orchestration flow**
   - After `Stage.PREPARE` and environment confirmation, run:
     - `pipeline.run_stage(Stage.REQUIREMENTS, ctx)` (or `PLAN`).
     - `pipeline.run_stage(Stage.DESIGN, ctx)` (or `REVIEW`).
   - Store artifacts from agent outputs in context (`ctx.requirements`, `ctx.design`) and persist them using `RunManager` (e.g., `save_context_file` or `save_json`).

3. **Adapt hints / context pack to understand requirements**
   - Modify `_collect_hints(ctx)` to also pull hints from:
     - `ctx.requirements` (e.g., requirement titles, ids).
     - `ctx.design` (component names, file paths).
   - This will help RepoScout focus on relevant files.

4. **Extend RepoScout to be requirements-aware**
   - Inside `RepoScout.gather` (not shown here), add logic to:
     - Use requirement keywords and design hints to prioritize search results.
   - No changes needed in `RepoScoutPlugin` itself beyond passing extra hints if needed.

**Result:**  
The pipeline becomes:  
`Env → Requirements → Design → Gather → Edit → Apply → Build → Test`, all automatically driven by `task`.

---

### Phase 3: Automated test generation and stronger policies

**Goal:** Move from “tests required if code changed” to “tests automatically generated/updated from requirements and design”.

1. **Introduce a TestAuthor agent**
   - New file: `agent/agents/test_author_plugin.py`.
   - Stage: could be:
     - `Stage.EDIT` (run before/after `PatchAuthorPlugin`), or
     - A new explicit stage (e.g., `Stage.VERIFY_TEST` pre-phase).
   - Input: `ctx.requirements`, `ctx.design`, `ctx.applied_files`, diagnostics.
   - Output:
     - Patches that add/update tests (via the same edit protocol and patch queue).
   - Implementation:
     - Very similar to `PatchAuthorPlugin`, but with:
       - Different prompt emphasizing test design and coverage.
       - Allowed files restricted to test directories/files.

2. **Refine `_check_test_coverage_needed`**
   - Instead of generic heuristics, incorporate:
     - Requirement IDs associated with changed code (traceability).
     - Test coverage metadata (e.g., which tests map to which requirements).
   - Use this to drive policy:
     - “For every requirement with changes, ensure at least one test is tied to it.”

3. **Make coverage policy configurable**
   - Add a `--enforce-tests` or similar flag; store in `ctx.policy`.
   - Allow “strict” vs “lenient” modes.

**Result:**  
The agent becomes capable of automatically **writing tests based on requirements**, then building and running them, forming a near-closed loop from requirement to validated behavior.

---

### Phase 4: Reduce human interaction for fully automatic runs

**Goal:** Make `agent do "task" --auto` a near-zero-interaction pipeline.

1. **Gate interactive prompts on `auto`**
   - In `Orchestrator.run()`:
     - Several places call `input(...)` when `auto` is false.
     - For full automation, ensure that when `auto=True`:
       - All prompts are skipped or auto-answered.
       - The loop continues until success, policy-satisfied or max_iters reached.

2. **Add a “requirements-only” and “full-auto” CLI mode**
   - Extend `build_parser()` in `agent/cli.py`:
     - Add subcommands:
       - `requirements`: generate & print requirements spec.
       - `auto`: full pipeline from requirements → design → code → tests (alias to `do` with extra options).
   - Wire them to orchestrator:
     - `requirements` → run only PREPARE + REQUIREMENTS stages and dump spec.
     - `auto` → run full pipeline with `auto=True`.

3. **Logging and artifacts**
   - Ensure all new artifacts (requirements, design, test plans) are saved via `RunManager`.
   - Provide a summary at the end of a run (e.g., which requirements were fully implemented and tested).

---

## 4. How my code-generation capability fits in

Given the above architecture:

- **LLMService** is already the main integration point.
- New agents (Requirements/Design/TestAuthor) will:
  - Construct structured prompts with:
    - `task`
    - repo context (`ctx.context_pack`)
    - previous artifacts (requirements, design, diffs)
  - Call specialized generation functions on `LLMService` or reuse `.generate_patch()` with different validation schemas:
    - For requirements/design: enforce JSON schema (no edits, just spec docs).
    - For patches/tests: keep current edit protocol but separate prompts.

- **Search/Replace editing protocol** (via `EditExecutor`) remains the safe mechanism to apply changes.

- My role as the LLM is to:
  - Produce high-quality, schema-valid artifacts (requirements, design, test plans).
  - Produce precise edits that respect your strict editing protocol.
  - Learn from build/test diagnostics in iterative loops to refine implementations.

---

## 5. Concrete next step (what to implement first)

Recommended immediate steps (low-risk, high-leverage):

1. **Add requirements & design fields to RunContext and persist them.**
2. **Create a new `RequirementsAgentPlugin`**:
   - Stage: `Stage.PLAN` (already in enum).
   - Use `LLMService` to output a JSON requirement spec from `ctx.task`.
   - Save as `requirements.json` under the run directory.
3. **Wire RequirementsAgent into `Orchestrator._make_pipeline()`**:
   - `reg.register(Stage.PLAN, RequirementsAgentPlugin())`.
4. **Adjust `Orchestrator.run()`**:
   - Before `Stage.GATHER`, run `pipeline.run_stage(Stage.PLAN, ctx)` once.
   - Include a simple print/log of requirements summary so you can inspect results.

Once that’s working, we can iterate on:

- Adding a `DesignAgent`.
- Feeding `ctx.requirements` & `ctx.design` into RepoScout and PatchAuthor prompts.
- Adding test-generation steps.

This staged approach keeps your current C demo working while progressively evolving into a fully automated requirements-driven development pipeline.
