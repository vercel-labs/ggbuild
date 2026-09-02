# Recipes and Sources

Recipes describe package intent while ggbuild owns source resolution, dependency solving, staging, and target-specific realization.

A recipe primarily declares identity, sources, runtime and build requirements, install inventory, build-system hooks, capabilities, metadata, and optional tests. Hook methods return scripts instead of directly mutating the host so the same recipe can feed several target builders.

## Registered Releases

Registered release instances are immutable planning inputs pairing a recipe class and source version with verified source metadata.

Constructing a bundled package registers it in the process-wide bundle repository. Project planning only selects exact instances with SHA-256 identity, which avoids network-dependent version discovery and ensures tests can recover the same recipe release that produced an artifact.

The low-level CLI still supports resolving an arbitrary source ref for development and unusual layouts. This compatibility path is intentionally broader than the registered-release contract used by [[orchestration#Canonical Build Plan]].

## Dependency Semantics

Runtime and build dependencies are solved separately because they have different effects on the shipped artifact and build ordering.

The Poetry solver is adapted to combine target-native repositories with the supplemental bundle repository. Target packages win when they satisfy constraints, allowing system libraries to remain external; bundled packages fill gaps or provide intentionally controlled versions.

### Runtime and Build Graphs

Runtime dependencies belong in the resulting installation, while build-only dependencies provide compilers, generators, headers, or tools without becoming runtime requirements.

Build requirements are stored alongside Poetry package objects and temporarily participate in incompatibility solving. The final build order uses the union of active runtime and build edges, but packaging and runtime-path logic retain the distinction.

### Declared Cycle Escape Hatch

A narrow recipe-level escape hatch handles genuine two-package build/runtime cycles that a topological build cannot otherwise order.

Only Python recipes that explicitly declare the cyclic runtime dependency can break such a cycle. The removed edge is reinserted immediately after the dependent package, leaving that recipe responsible for bootstrapping its dependency through mechanisms such as `PYTHONPATH`.

## Verified Source Acquisition

Source acquisition prefers immutable, verified bytes and degrades predictably across transient network failures and mirrors.

HTTP sources retry connection failures and selected transient statuses with bounded backoff, honor a bounded `Retry-After`, and try deduplicated mirrors. Cached or newly downloaded files are accepted only after every configured verifier passes; partial or invalid files are deleted.

Archive, Git, and local sources converge on a source tree or target-owned tarball. Git sources can preserve selected submodules or repository metadata when a recipe needs them, while local sources exclude version-control state from generated archives.

## Safe Extraction

Source extraction supports target toolchains without allowing archive entries to escape the destination.

When a build object is available, target `tar` is used so extraction semantics match the target environment and progress remains visible. The Python fallback rejects absolute paths, parent traversal, unsafe links, devices, and unsupported members before extraction; zip paths receive equivalent checks.

## Script-Oriented Build Hooks

Recipe hooks form a staged shell contract so package intent can be composed with target paths, flags, tools, and dependency environments.

The major phases are prepare, configure, build, test-build, build-install, test-install, and final install selection. Built-in Autoconf, CMake, Meson, Make, Python, Rust, and Go helpers encode common conventions while retaining hook overrides for exceptional projects.

Argument values prefixed with `!` are already shell expressions. Flag combinators preserve this distinction to avoid double quoting when composing environment variables, paths, C/C++ flags, linker flags, and Rust linker arguments.
