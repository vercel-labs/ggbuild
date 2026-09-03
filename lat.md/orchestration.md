# Project Orchestration

The project layer turns declarative policy and registered releases into the same validated node graph for local execution and GitHub Actions.

## Project and Target Policy

`[tool.ggbuild]` names the root recipe, release-selection policy, target triples, build options, cache policy, workflow settings, and optional publication destination.

Target triples supply conservative defaults: Linux uses target-owned Docker environments, macOS and Windows use matching hosted runners, and macOS sets a deployment floor. Projects may override runner, execution mode, and environment without replacing the target's packaging behavior.

## Canonical Build Plan

The versioned JSON plan is the authority shared by planning, local scheduling, CI jobs, cache lookup, testing, and publication.

For every target and selected root release, the planner resolves the registered bundled dependency closure and creates three node roles: `bundle` for one dependency installation, `artifact` for a root distribution, and optional `test` for its recipe hook.

Nodes record sorted direct, runtime, build, and artifact edges. Roots are terminal artifact or test nodes with their complete closure and a digest over closure cache keys. Canonical JSON and sorted collections make semantically equal plans byte-stable.

### Plan Validation

Plans are validated as an untrusted protocol before execution or persistence.

Validation rejects unknown versions and roles, duplicate or unsorted nodes, cycles, stale topological layers, unsafe output paths, inconsistent edge classifications, malformed installation paths, incorrect roots, and stale cache or closure digests.

### Cache Identity

A node key identifies content-producing inputs rather than scheduling policy or incidental execution state.

It covers verified package source metadata, the recipe tree, target and container policy, build options, bundle prefix, direct dependency keys, project policy, and ggbuild's installed identity. sccache policy is excluded because it should only accelerate an otherwise identical build.

For editable ggbuild installs, identity includes version, Git revision, and tracked plus untracked dirty content. `GGBUILD_IGNORE_DIRTY_STATE` omits only the dirty component for deliberate local experiments.

## Low-Level Build Boundary

Node execution reconstructs a low-level `ggbuild build` invocation instead of bypassing recipe solving and target builders.

Before invocation, dependency bundles are merged into a node sysroot and their package identities are exported as prebuilt. The low-level solver still resolves native packages and the full recipe context, but known bundled dependencies use the staged installations and node output is redirected into that sysroot.

Artifact and test nodes cross a different boundary: artifacts are copied into target/version directories, while tests consume the exact artifact directory produced by their sole artifact dependency.

## Delta Sysroot Bundles

A bundle contains only files introduced by one dependency node, not the entire transitive sysroot used to build it.

Execution first merges dependency bundles, snapshots that baseline, runs the package build into the same staging tree, and exports the manifest difference plus required parent directories. This keeps independently cached bundles composable and avoids repeatedly shipping transitive content.

Bundles are deterministic zstd-compressed tar archives with normalized ownership, timestamps, ordering, modes, symlinks, and a canonical manifest.

## Bundle Validation and Merge

Restoring a bundle verifies both protocol identity and every extracted filesystem entry before publishing it atomically.

Absolute paths, traversal, hard links, escaping symlinks, unsupported entries, metadata mismatches, digest mismatches, and corrupt compression are rejected. Extraction happens in a temporary sibling and replaces the destination only after manifest equality is proven.

Merging permits identical directories, symlinks, and file bytes but rejects conflicting content or types. This makes dependency overlap explicit instead of depending on merge order.

## Layered Scheduling and Cache Policy

The scheduler runs topological layers sequentially and independent nodes within each layer concurrently under a configured bound.

Only bundle nodes are cacheable. `auto` requires the exact key, `ignore` rebuilds despite a hit, and `reuse` may accept the newest bundle whose structural identity matches apart from the key. A reused bundle is fully validated and republished under the current key before downstream execution.

Artifact and test nodes always run, so tests cannot be skipped by stale success and publication always observes current outputs. Failed nodes never produce a valid cache entry.

## Host and Container Execution

Execution policy distinguishes exact native hosts from Linux containers that may use Docker's architecture emulation.

Host builds require matching OS and hardware. Prepared CI images require an exact target triple because they already embody the target environment; ordinary Docker launches allow a different host architecture through the selected platform.

Linux containers run as the invoking non-root UID/GID, receive matching passwd/group records, mount recipe and tool code read-only, isolate work and home paths, and expose only intentional cache, output, and credential channels.

## Content-Addressed Linux Environments

Linux build and ABI-test images are rendered from target-owned templates and pinned base inputs.

The environment identity includes the template, rendered Dockerfile, platform, images, variables, and verified sccache payloads. Registry action tags hash the complete action Dockerfile and entrypoint, so CI cannot silently run a mutable build environment.

GNU uses a Rocky Linux 8 glibc baseline; musl uses a stripped Alpine build root. Minimal test images are separate because [[artifacts#ABI-Floor Linux Tests|runtime validation]] should not inherit build tools or libraries.

## Workflow as Plan Projection

The generated GitHub Actions workflow projects the canonical plan's target-scoped topological layers into bounded matrices.

Each matrix job depends on the jobs containing its direct prerequisites. Bundle artifacts are handed across jobs by content key, root artifacts cross the explicit artifact-to-test boundary, and each child executes exactly one assigned node so it cannot redundantly schedule its closure.

The planning job records an expected plan digest, generated files are checked byte-for-byte, action revisions are pinned, obsolete pull-request runs are cancelled, and publication depends on every build matrix succeeding.

Manual dispatches may test host and Linux execution with a full ggbuild commit SHA. The static plan remains pinned, persistent bundle-cache reuse and publication are disabled, and normal runs enforce the generated revision and plan digest.

Downstream index ingestion authenticates with a GitHub OIDC token scoped to the configured HTTPS audience. Protected Vercel endpoints may additionally name a GitHub Actions secret whose value is sent as the Vercel protection-bypass header for both publication and retry ingestion.
