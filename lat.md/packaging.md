# Target-Native Packaging

The target layer translates recipe intent into a platform-compatible build, installation image, and final distribution.

## Target Abstraction

A target owns platform identity, package discovery, filesystem layout, compiler and linker policy, binary inspection, service integration, and the concrete builder.

Host detection chooses a target from OS, architecture, libc, distribution, and the portable/native choice. Recipes therefore refer to semantic install aspects such as `lib`, `include`, or `systembin` instead of hard-coding each platform's paths.

## Portable and Native Outputs

Portable targets create self-contained generic archives, while native targets generate the package-manager metadata expected by the host platform.

Generic Linux and macOS produce relocatable archive layouts. Debian and RPM builders translate the same dependency and install model into control/spec metadata and invoke `dpkg-buildpackage` or `rpmbuild`; native macOS emits installer packages. Windows currently follows the generic portable path.

Portable does not mean statically linked. The builder bundles required non-system shared libraries, writes controlled runtime paths, and permits only a target-defined system-library baseline.

## Build Lifecycle and Staging

Each low-level build moves through preparation, source materialization, recipe stages, packaging preparation, target packaging, and artifact export.

All recipe packages share one build state and path model, but each package gets isolated source, build, install, temporary, and test-install locations. In orchestrated node mode the installation image is redirected into a supplied sysroot so it can become a dependency bundle.

The lifecycle always stops an isolated sccache server in a `finally` path, making compiler-cache acceleration subordinate to build correctness.

## Dependency Wiring

Bundled dependencies are staged under target-aware prefixes and exposed only through generated build environments.

ggbuild derives `PATH`, `PKG_CONFIG_PATH`, `PKG_CONFIG_SYSROOT_DIR`, `CMAKE_PREFIX_PATH`, include flags, link paths, and runtime paths from recipe capabilities and dependency roles. Build-only libraries contribute at link time but are not written into the shipped runtime search path.

This explicit wiring prevents undeclared host libraries from silently influencing a supposedly portable result and allows restored [[orchestration#Delta Sysroot Bundles|bundle sysroots]] to behave like locally built dependencies.

## Binary Closure and Relocatability

Generic packaging inspects every produced binary to prove that dynamic-library references resolve to allowed system libraries or files in the artifact.

Target implementations read shared-library references and runtime paths, then rewrite rpaths or install names as needed. Symlinked libraries used by binaries are materialized; unused real targets and development symlinks are removed to make the runtime closure unambiguous.

Binary post-processing preserves original modification times so stripping and path repair do not make rebuilds appear newer than compilation. Optional debug symbols are extracted before the primary binary is stripped; see [[artifacts#Artifact Inventory and Sidecars]].

## Validated Resume

`--keepwork` resumes expensive builds through stage checkpoints only after comparing retained inputs with a fresh build manifest.

The manifest fingerprints ggbuild, target and build options, dependencies, recipe code, source verifiers, patches, and rendered scripts. Changes invalidate from the earliest affected package stage; source changes invalidate unpacking, while compression-only changes invalidate final assembly.

Per-recipe `ignore` forces invalidation and `reuse` accepts explicitly stale package work. Stale reuse is recorded in the next manifest so a later automatic run cannot mistake it for an exact checkpoint.

## Compiler Cache Isolation

sccache is optional acceleration with shared object storage but a server endpoint isolated per build directory.

ggbuild wraps C, C++, qualified GCC/Clang names, and Rust without wrapping ccache or recursively invoking itself. Credentials are forwarded only as environment references, and server statistics plus shutdown are attempted even when the build fails.

## Build Output Observability

Build logs retain exact subprocess output while the live status reduces noisy compiler and tool invocations to stable, path-aware summaries.

The parser understands nested Make directories, cross compiler names, shell/libtool wrappers, Windows paths, and common archive/install tools. It changes only structured status detail, preserving the original message for diagnostics.
